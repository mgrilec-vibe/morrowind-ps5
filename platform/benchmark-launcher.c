#include <sys/types.h>
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/proc.h>
#include <sys/socket.h>
#include <sys/sysctl.h>
#include <sys/time.h>
#include <sys/user.h>
#include <unistd.h>

#define PORT 9138
#define MAX_REQUEST 32
#define MAX_PROC_BYTES (16u * 1024u * 1024u)
#define RESPONSE_SIZE 384
#define TITLE_ID "PPSA99630"
#define SERVICE "openmw-launcher"
/* FreeBSD's MSG_NOSIGNAL value used by etaHEN's PS5 daemon. */
#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0x20000
#endif

typedef struct {
  unsigned char bytes[256];
} app_info_t;

int sceKernelGetAppInfo(pid_t pid, app_info_t *info);

static int
app_info_has_title(const app_info_t *info) {
  /* Probe on the target console confirmed the NUL-terminated title at byte 16. */
  return memcmp(info->bytes + 16, TITLE_ID, sizeof(TITLE_ID)) == 0;
}

/* Layout and enum values from etaHEN/etaHEN Source Code/daemon/include/launcher.hpp. */
typedef uint64_t lnc_check_flag_t;
enum { LNC_FLAG_NONE = 0 };
typedef struct {
  uint32_t sz;
  int user_id;
  uint32_t app_opt;
  uint64_t crash_report;
  lnc_check_flag_t check_flag;
} lnc_app_param_t;

typedef int (*get_foreground_user_fn)(int *user_id);
typedef int (*launch_app_fn)(const char *title_id, const char *argv[], lnc_app_param_t *param);

typedef int (*user_service_initialize_fn)(void *param);

static void *user_service_module;
static void *system_service_module;
static user_service_initialize_fn initialize_user_service;
static get_foreground_user_fn get_foreground_user;
static launch_app_fn launch_title;
static int launch_api_state;
static int launch_api_error;

static int
initialize_launch_api(void) {
  if (launch_api_state == 1) return 0;
  if (launch_api_state == -1) return launch_api_error;

  user_service_module = dlopen("libSceUserService.sprx", RTLD_LAZY);
  if (!user_service_module) {
    launch_api_error = ENOENT;
    launch_api_state = -1;
    return launch_api_error;
  }
  system_service_module = dlopen("libSceSystemService.sprx", RTLD_LAZY);
  if (!system_service_module) {
    launch_api_error = ENOENT;
    launch_api_state = -1;
    return launch_api_error;
  }

  initialize_user_service =
      (user_service_initialize_fn)dlsym(user_service_module, "sceUserServiceInitialize");
  get_foreground_user =
      (get_foreground_user_fn)dlsym(user_service_module, "sceUserServiceGetForegroundUser");
  launch_title = (launch_app_fn)dlsym(system_service_module, "sceLncUtilLaunchApp");
  if (!initialize_user_service || !get_foreground_user || !launch_title) {
    launch_api_error = ENOSYS;
    launch_api_state = -1;
    return launch_api_error;
  }

  launch_api_error = initialize_user_service(NULL);
  if (launch_api_error != 0) {
    launch_api_state = -1;
    return launch_api_error;
  }
  launch_api_state = 1;
  return 0;
}

static char *process_data;
static size_t process_capacity;

static void
cleanup_process_data(void) {
  free(process_data);
  process_data = NULL;
  process_capacity = 0;
}

static void
response(int fd, int ok, const char *details) {
  char buf[RESPONSE_SIZE];
  int len = snprintf(buf, sizeof(buf),
                     "{\"service\":\"%s\",\"protocol\":1,\"title_id\":\"%s\",\"ok\":%s%s}\n",
                     SERVICE, TITLE_ID, ok ? "true" : "false", details ? details : "");
  if (len > 0 && (size_t)len < sizeof(buf)) {
    size_t sent = 0;
    while (sent < (size_t)len) {
      ssize_t n = send(fd, buf + sent, (size_t)len - sent, MSG_NOSIGNAL);
      if (n > 0) sent += (size_t)n;
      else if (n < 0 && errno == EINTR) continue;
      else break;
    }
  }
}

static void
error_response(int fd, const char *message, int code) {
  char extra[160];
  int n = snprintf(extra, sizeof(extra), ",\"error\":\"%s\",\"code\":%d", message, code);
  response(fd, 0, n > 0 && (size_t)n < sizeof(extra) ? extra : ",\"error\":\"internal error\"");
}

/* Returns a negative errno-style value on failed or truncated enumeration. */
static int
title_process(pid_t *pid_out) {
  int mib[4] = { CTL_KERN, KERN_PROC, KERN_PROC_PROC, 0 };
  size_t size = 0;
  if (sysctl(mib, 4, NULL, &size, NULL, 0) != 0) return -errno;
  if (size == 0 || size > MAX_PROC_BYTES) return -EOVERFLOW;

  if (size > process_capacity) {
    char *next = realloc(process_data, size);
    if (!next) return -ENOMEM;
    process_data = next;
    process_capacity = size;
  }

  size_t used = 0;
  for (;;) {
    used = process_capacity;
    if (sysctl(mib, 4, process_data, &used, NULL, 0) == 0) break;
    int e = errno;
    if (e != ENOMEM) return -e;
    if (process_capacity >= MAX_PROC_BYTES) return -EOVERFLOW;
    size_t grown = process_capacity > MAX_PROC_BYTES / 2 ?
        MAX_PROC_BYTES : process_capacity * 2;
    if (grown <= process_capacity) return -EOVERFLOW;
    char *next = realloc(process_data, grown);
    if (!next) return -ENOMEM;
    process_data = next;
    process_capacity = grown;
  }
  if (used == 0 || used > process_capacity) return -EOVERFLOW;

  pid_t found = 0;
  size_t offset = 0;
  while (offset < used) {
    if (used - offset < sizeof(struct kinfo_proc)) return -EOVERFLOW;
    struct kinfo_proc *ki = (struct kinfo_proc *)(void *)(process_data + offset);
    size_t record_size = ki->ki_structsize;
    if (record_size < sizeof(struct kinfo_proc) || record_size > used - offset) {
      return -EOVERFLOW;
    }
    app_info_t info;
    memset(&info, 0, sizeof(info));
    /* The SDK ps sample uses sceKernelGetAppInfo to obtain title_id per PID. */
    int app_result = sceKernelGetAppInfo(ki->ki_pid, &info);
    if (app_result != 0) {
      /* The live console probe established ESRCH for an exited PID (enumeration race). */
      if ((uint32_t)app_result != UINT32_C(0x80020003)) return -EIO;
    } else if (app_info_has_title(&info)) {
      if (found != 0) return -EEXIST;
      found = ki->ki_pid;
    }
    offset += record_size;
  }
  if (offset != used) return -EOVERFLOW;
  *pid_out = found;
  return 0;
}

static int
read_request(int fd, char request[MAX_REQUEST]) {
  size_t used = 0;
  while (used < MAX_REQUEST) {
    char ch;
    ssize_t n = recv(fd, &ch, 1, 0);
    if (n == 0) return -1;
    if (n < 0) {
      if (errno == EINTR) continue;
      return -1;
    }
    if (ch == '\n') {
      request[used] = '\0';
      return used == 0 ? -1 : 0;
    }
    if ((unsigned char)ch < 0x20 || (unsigned char)ch > 0x7e || ch == '\r') return -1;
    request[used++] = ch;
  }
  return -1;
}

static void
handle_client(int fd, int *should_exit) {
  char request[MAX_REQUEST];
  if (read_request(fd, request) != 0) {
    error_response(fd, "invalid request", EINVAL);
    return;
  }
  if (strcmp(request, "STATUS") == 0) {
    pid_t pid = 0;
    int result = title_process(&pid);
    if (result != 0) {
      error_response(fd, "process enumeration failed", -result);
      return;
    }
    char extra[96];
    snprintf(extra, sizeof(extra), ",\"running\":%s,\"pid\":%d", pid ? "true" : "false", (int)pid);
    response(fd, 1, extra);
    return;
  }
  if (strcmp(request, "LAUNCH") == 0) {
    pid_t pid = 0;
    int result = title_process(&pid);
    if (result != 0) {
      error_response(fd, "process enumeration failed", -result);
      return;
    }
    if (pid) {
      error_response(fd, "title already running", EALREADY);
      return;
    }
    result = initialize_launch_api();
    if (result != 0) {
      error_response(fd, "launch API initialization failed", result);
      return;
    }
    int user_id = 0;
    result = get_foreground_user(&user_id);
    if (result != 0) {
      error_response(fd, "foreground user lookup failed", result);
      return;
    }
    lnc_app_param_t param = { sizeof(lnc_app_param_t), user_id, 0, 0, LNC_FLAG_NONE };
    int launch_result = launch_title(TITLE_ID, NULL, &param);
    if (launch_result < 0) {
      error_response(fd, "native launch failed", launch_result);
      return;
    }
    char extra[64];
    snprintf(extra, sizeof(extra), ",\"launch_result\":%d", launch_result);
    response(fd, 1, extra);
    return;
  }
  if (strcmp(request, "EXIT") == 0) {
    *should_exit = 1;
    response(fd, 1, NULL);
    return;
  }
  error_response(fd, "unknown command", EINVAL);
}

int
main(void) {
  signal(SIGPIPE, SIG_IGN);
  if (atexit(cleanup_process_data) != 0) return 1;
  int server = socket(AF_INET, SOCK_STREAM, 0);
  if (server < 0) return 1;
  int reuse = 1;
  (void)setsockopt(server, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
  struct sockaddr_in address;
  memset(&address, 0, sizeof(address));
  address.sin_len = sizeof(address);
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_ANY);
  address.sin_port = htons(PORT);
  if (bind(server, (struct sockaddr *)&address, sizeof(address)) != 0 || listen(server, 4) != 0) {
    close(server);
    return 1;
  }

  for (;;) {
    int client = accept(server, NULL, NULL);
    if (client < 0) {
      if (errno == EINTR) continue;
      close(server);
      return 1;
    }
    struct timeval timeout = { 3, 0 };
    (void)setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    (void)setsockopt(client, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    int should_exit = 0;
    handle_client(client, &should_exit);
    close(client);
    if (should_exit) break;
  }
  close(server);
  return 0;
}
