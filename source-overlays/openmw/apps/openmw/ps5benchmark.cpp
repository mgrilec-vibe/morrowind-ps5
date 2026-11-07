#include "ps5benchmark.hpp"

#include <yaml-cpp/yaml.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <cerrno>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <limits>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <type_traits>
#include <vector>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>

namespace MWPS5Benchmark
{
namespace
{
    using Clock = std::chrono::steady_clock;
    constexpr size_t MaxConfigBytes = 64 * 1024;
    constexpr size_t MaxScenarioBytes = 1024 * 1024;
    constexpr size_t MaxFrames = 1000000;
    constexpr size_t MaxEvents = 4096;
    constexpr size_t MaxCheckpoints = 1024;
    constexpr size_t QueueCapacity = 32;
    constexpr size_t ProtocolLineBytes = 65536;
    constexpr double MaxDurationSeconds = 3600.0;

    enum class CaptureState { Idle, Loading, WaitingWorld, Warming, Capturing, Complete, Incomplete, Error };
    enum class CommandType { LoadSave, RunScenario, Stop, Input, Quit };

    struct FrameRecord
    {
        uint64_t frameId = 0;
        uint64_t scenarioTimeNs = 0;
        uint64_t outerNs = 0;
        uint64_t updateNs = 0;
        uint64_t renderNs = 0;
        uint64_t luaWaitNs = 0;
        uint64_t limiterNs = 0;
        int32_t checkpoint = -1;
        uint32_t cellId = 0;
        float x = 0.f, y = 0.f, z = 0.f, rotX = 0.f, rotY = 0.f, rotZ = 0.f;
        uint8_t phase = 0, paused = 0, loading = 0;
    };

    struct Event
    {
        uint64_t timeNs = 0;
        InputState input;
    };

    struct Checkpoint
    {
        uint64_t timeNs = 0;
        float x = 0.f, y = 0.f, z = 0.f, tolerance = 0.f;
        float rotX = 0.f, rotY = 0.f, rotZ = 0.f, rotationTolerance = 0.f;
        bool hasPose = false;
        bool hasRotation = false;
        std::string cell;
        bool hasCell = false;
    };

    struct Scenario
    {
        std::string filename;
        std::string save;
        uint64_t warmupNs = 0;
        uint64_t durationNs = 0;
        std::vector<Event> events;
        std::vector<Checkpoint> checkpoints;
    };

    struct Command
    {
        CommandType type = CommandType::Stop;
        uint64_t id = 0;
        std::string name;
        InputState input;
        uint32_t leaseMs = 0;
    };

    struct Config
    {
        bool enabled = false;
        bool controlEnabled = false;
        bool captureEnabled = false;
        std::string bind = "0.0.0.0";
        uint16_t port = 9137;
        std::string token;
        size_t maxFrames = 36000;
        std::string startupScenario;
        std::string error;
    };

    struct Runtime
    {
        Config config;
        Callbacks callbacks;
        std::filesystem::path workspace;
        bool started = false;
        bool shutdownRequested = false;
        bool scenarioLoaded = false;
        bool startupAttempted = false;
        bool standalonePending = false;
        bool loadAwaitingReady = false;
        bool loadSawNotReady = false;
        bool haveSnapshot = false;
        bool pendingRecordValid = false;
        bool inputIsActive = false;
        bool inputIsManual = false;
        std::atomic<bool> inputSnapshotActive{false};
        std::atomic<bool> disconnectPending{false};
        std::mutex inputMutex;
        InputState inputSnapshot;
        bool networkRun = false;
        bool runInvalid = false;
        bool outputFinalized = false;
        bool ready = false;
        bool loading = false;
        bool paused = false;
        std::string loadedSave;
        double simulationOrigin = 0.0;
        double simulationNow = 0.0;
        float playerX = 0.f, playerY = 0.f, playerZ = 0.f;
        float playerRotX = 0.f, playerRotY = 0.f, playerRotZ = 0.f;
        std::thread worker;
        std::atomic<bool> stopWorker{false};
        std::atomic<int> listenSocket{-1};
        std::atomic<int> workerError{0};
        std::mutex queueMutex;
        std::array<Command, QueueCapacity> queue;
        size_t queueHead = 0, queueSize = 0;
        std::mutex statusMutex;
        struct PublishedStatus
        {
            bool ready = false;
            CaptureState capture = CaptureState::Idle;
            bool completed = false;
            std::string gameState = "starting";
            std::string runId;
            std::string manifest;
            std::string frames;
            std::string loadedSave;
            std::string error;
            uint64_t lastCommandId = 0;
            std::string lastCommandError;
        } published;
        std::string gameState = "starting";
        std::string lastError;
        uint64_t lastCommandId = 0;
        std::string lastCommandError;
        std::string runId;
        std::string runPath;
        std::string manifestFile;
        std::string framesFile;
        CaptureState capture = CaptureState::Idle;
        bool completed = false;
        Scenario scenario;
        std::vector<FrameRecord> frames;
        FrameRecord pendingRecord;
        uint64_t previousFrameStart = 0;
        uint64_t frameStartNs = 0;
        uint64_t pendingStartNs = 0;
        uint64_t runStartedNs = 0;
        uint64_t captureStartedNs = 0;
        uint64_t warmupStartNs = 0;
        uint64_t measuredElapsedNs = 0;
        uint64_t scenarioTimeNs = 0;
        size_t nextEvent = 0;
        size_t nextCheckpoint = 0;
        int32_t activeCheckpoint = -1;
        uint32_t cellId = 0;
        std::string cellName;
        InputState currentInput;
        Clock::time_point leaseDeadline{};
    } g;

    uint64_t nowNs()
    {
        return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch()).count());
    }

    bool safeName(const std::string& name)
    {
        if (name.empty() || name.size() > 96 || name == "." || name == "..") return false;
        for (unsigned char c : name)
            if (!(std::isalnum(c) || c == '_' || c == '-' || c == '.')) return false;
        return name.find("..") == std::string::npos;
    }

    bool safeFilename(const std::string& name)
    {
        if (!safeName(name) || name.find('/') != std::string::npos || name.find('\\') != std::string::npos) return false;
        return (name.size() > 4 && name.compare(name.size() - 4, 4, ".omw") == 0)
            || (name.size() > 8 && name.compare(name.size() - 8, 8, ".omwsave") == 0);
    }

    bool readBounded(const std::filesystem::path& path, size_t maximum, std::string& contents)
    {
        std::error_code ec;
        const uintmax_t size = std::filesystem::file_size(path, ec);
        if (ec || size > maximum) return false;
        std::ifstream stream(path, std::ios::binary);
        if (!stream) return false;
        contents.resize(static_cast<size_t>(size));
        if (size && !stream.read(&contents[0], static_cast<std::streamsize>(size))) return false;
        return true;
    }

    class JsonValidator
    {
        const std::string& mText;
        size_t mPosition = 0;
        void whitespace()
        {
            while (mPosition < mText.size() && (mText[mPosition] == ' ' || mText[mPosition] == '\t'
                || mText[mPosition] == '\r' || mText[mPosition] == '\n')) ++mPosition;
        }
        bool string()
        {
            if (mPosition >= mText.size() || mText[mPosition++] != '"') return false;
            while (mPosition < mText.size())
            {
                const unsigned char c = mText[mPosition++];
                if (c == '"') return true;
                if (c < 0x20) return false;
                if (c == '\\')
                {
                    if (mPosition >= mText.size()) return false;
                    const char escape = mText[mPosition++];
                    if (escape == 'u')
                    {
                        for (int i = 0; i < 4; ++i)
                            if (mPosition >= mText.size() || !std::isxdigit(static_cast<unsigned char>(mText[mPosition++]))) return false;
                    }
                    else if (escape != '"' && escape != '\\' && escape != '/' && escape != 'b' && escape != 'f'
                        && escape != 'n' && escape != 'r' && escape != 't') return false;
                }
            }
            return false;
        }
        bool number()
        {
            const size_t start = mPosition;
            if (mPosition < mText.size() && mText[mPosition] == '-') ++mPosition;
            if (mPosition >= mText.size()) return false;
            if (mText[mPosition] == '0') ++mPosition;
            else
            {
                if (mText[mPosition] < '1' || mText[mPosition] > '9') return false;
                while (mPosition < mText.size() && mText[mPosition] >= '0' && mText[mPosition] <= '9') ++mPosition;
            }
            if (mPosition < mText.size() && mText[mPosition] == '.')
            {
                ++mPosition;
                const size_t digits = mPosition;
                while (mPosition < mText.size() && mText[mPosition] >= '0' && mText[mPosition] <= '9') ++mPosition;
                if (digits == mPosition) return false;
            }
            if (mPosition < mText.size() && (mText[mPosition] == 'e' || mText[mPosition] == 'E'))
            {
                ++mPosition;
                if (mPosition < mText.size() && (mText[mPosition] == '+' || mText[mPosition] == '-')) ++mPosition;
                const size_t digits = mPosition;
                while (mPosition < mText.size() && mText[mPosition] >= '0' && mText[mPosition] <= '9') ++mPosition;
                if (digits == mPosition) return false;
            }
            return mPosition > start;
        }
        bool value(unsigned depth)
        {
            if (depth > 16) return false;
            whitespace();
            if (mPosition >= mText.size()) return false;
            const char c = mText[mPosition];
            if (c == '"') return string();
            if (c == '{')
            {
                ++mPosition; whitespace();
                if (mPosition < mText.size() && mText[mPosition] == '}') { ++mPosition; return true; }
                for (;;)
                {
                    whitespace();
                    if (!string()) return false;
                    whitespace();
                    if (mPosition >= mText.size() || mText[mPosition++] != ':') return false;
                    if (!value(depth + 1)) return false;
                    whitespace();
                    if (mPosition >= mText.size()) return false;
                    const char separator = mText[mPosition++];
                    if (separator == '}') return true;
                    if (separator != ',') return false;
                }
            }
            if (c == '[')
            {
                ++mPosition; whitespace();
                if (mPosition < mText.size() && mText[mPosition] == ']') { ++mPosition; return true; }
                for (;;)
                {
                    if (!value(depth + 1)) return false;
                    whitespace();
                    if (mPosition >= mText.size()) return false;
                    const char separator = mText[mPosition++];
                    if (separator == ']') return true;
                    if (separator != ',') return false;
                }
            }
            if (c == '-' || (c >= '0' && c <= '9')) return number();
            if (mText.compare(mPosition, 4, "true") == 0) { mPosition += 4; return true; }
            if (mText.compare(mPosition, 5, "false") == 0) { mPosition += 5; return true; }
            if (mText.compare(mPosition, 4, "null") == 0) { mPosition += 4; return true; }
            return false;
        }
    public:
        explicit JsonValidator(const std::string& text) : mText(text) {}
        bool valid()
        {
            whitespace();
            if (mPosition >= mText.size() || mText[mPosition] != '{' || !value(0)) return false;
            whitespace();
            return mPosition == mText.size();
        }
    };

    bool requiredBool(const YAML::Node& node, const char* key, bool& result)
    {
        if (!node[key] || !node[key].IsScalar()) return false;
        try { result = node[key].as<bool>(); return true; } catch (...) { return false; }
    }

    template <typename T>
    bool boundedNumber(const YAML::Node& node, const char* key, T minimum, T maximum, T& result)
    {
        if (!node[key] || !node[key].IsScalar()) return false;
        try
        {
            result = node[key].as<T>();
            if (result < minimum || result > maximum) return false;
            if constexpr (std::is_floating_point<T>::value) return std::isfinite(result);
            return true;
        }
        catch (...) { return false; }
    }

    bool parseConfig(const std::filesystem::path& path, Config& config)
    {
        std::error_code ec;
        if (!std::filesystem::exists(path, ec)) return true;
        std::string text;
        if (!readBounded(path, MaxConfigBytes, text)) { config.error = "config_unreadable_or_oversized"; return false; }
        try
        {
            if (!JsonValidator(text).valid()) throw std::runtime_error("config_json");
            YAML::Node root = YAML::Load(text);
            if (!root.IsMap() || !root["version"] || root["version"].as<int>() != 1 || !requiredBool(root, "enabled", config.enabled))
                throw std::runtime_error("config_schema");
            const YAML::Node control = root["control"];
            if (control)
            {
                if (!control.IsMap() || !requiredBool(control, "enabled", config.controlEnabled)) throw std::runtime_error("control_schema");
                if (control["bind"]) config.bind = control["bind"].as<std::string>();
                if (config.bind.empty() || config.bind.size() > 64) throw std::runtime_error("control_bind");
                unsigned port = config.port;
                if (control["port"] && (!boundedNumber(control, "port", 1u, 65535u, port))) throw std::runtime_error("control_port");
                config.port = static_cast<uint16_t>(port);
                if (config.controlEnabled)
                {
                    if (!control["token"] || !control["token"].IsScalar()) throw std::runtime_error("control_token");
                    config.token = control["token"].as<std::string>();
                    if (config.token.size() < 32 || config.token.size() > 128) throw std::runtime_error("control_token_length");
                    for (unsigned char ch : config.token)
                        if (!std::isxdigit(ch)) throw std::runtime_error("control_token_hex");
                }
            }
            const YAML::Node capture = root["capture"];
            if (capture)
            {
                if (!capture.IsMap() || !requiredBool(capture, "enabled", config.captureEnabled)) throw std::runtime_error("capture_schema");
                unsigned maxFrames = 36000;
                if (capture["max_frames"] && !boundedNumber(capture, "max_frames", 1u, static_cast<unsigned>(MaxFrames), maxFrames))
                    throw std::runtime_error("capture_max_frames");
                config.maxFrames = maxFrames;
            }
            if (root["startup_scenario"])
            {
                config.startupScenario = root["startup_scenario"].as<std::string>();
                if (!config.startupScenario.empty() && !safeName(config.startupScenario)) throw std::runtime_error("startup_scenario_name");
            }
            if (!config.enabled) return true;
            if (!config.captureEnabled && !config.controlEnabled && config.startupScenario.empty())
                throw std::runtime_error("enabled_without_feature");
            return true;
        }
        catch (const std::exception& e)
        {
            config.enabled = config.controlEnabled = config.captureEnabled = false;
            config.error = e.what();
            return false;
        }
    }

    bool parseInput(const YAML::Node& node, InputState& input)
    {
        if (!node.IsMap()) return false;
        input = InputState{};
        auto button = [&](const char* name, bool& value) {
            return !node[name] || requiredBool(node, name, value);
        };
        auto axis = [&](const char* name, float& value) {
            if (!node[name]) return true;
            return boundedNumber(node, name, -1.f, 1.f, value);
        };
        return axis("forward", input.forward) && axis("strafe", input.strafe) && axis("look_x", input.lookX) && axis("look_y", input.lookY)
            && button("jump", input.jump) && button("activate", input.activate) && button("attack", input.attack)
            && button("run", input.run) && button("sneak", input.sneak);
    }

    bool parseScenario(const std::string& filename, Scenario& scenario)
    {
        if (!safeName(filename)) return false;
        std::string text;
        if (!readBounded(g.workspace / "scenarios" / filename, MaxScenarioBytes, text)) return false;
        try
        {
            if (!JsonValidator(text).valid()) return false;
            YAML::Node root = YAML::Load(text);
            if (!root.IsMap() || !root["version"] || root["version"].as<int>() != 1 || !root["save"] || !root["save"].IsScalar()) return false;
            Scenario parsed;
            parsed.filename = filename;
            parsed.save = root["save"].as<std::string>();
            if (!safeFilename(parsed.save)) return false;
            double warmup = 0, duration = 0;
            if (!boundedNumber(root, "warmup_seconds", 0.0, MaxDurationSeconds, warmup)
                || !boundedNumber(root, "duration_seconds", 0.001, MaxDurationSeconds, duration)) return false;
            parsed.warmupNs = static_cast<uint64_t>(warmup * 1e9);
            parsed.durationNs = static_cast<uint64_t>(duration * 1e9);
            const YAML::Node events = root["events"];
            if (!events || !events.IsSequence() || events.size() > MaxEvents) return false;
            uint64_t previous = 0;
            for (const YAML::Node& entry : events)
            {
                double time = 0;
                Event event;
                if (!entry.IsMap() || !boundedNumber(entry, "time", 0.0, duration, time) || !entry["input"]
                    || !parseInput(entry["input"], event.input)) return false;
                event.timeNs = static_cast<uint64_t>(time * 1e9);
                if (!parsed.events.empty() && event.timeNs < previous) return false;
                previous = event.timeNs;
                parsed.events.push_back(event);
            }
            const YAML::Node checkpoints = root["checkpoints"];
            if (checkpoints)
            {
                if (!checkpoints.IsSequence() || checkpoints.size() > MaxCheckpoints) return false;
                previous = 0;
                for (const YAML::Node& entry : checkpoints)
                {
                    double time = 0;
                    Checkpoint point;
                    if (!entry.IsMap() || !boundedNumber(entry, "time", 0.0, duration, time)) return false;
                    point.timeNs = static_cast<uint64_t>(time * 1e9);
                    if (!parsed.checkpoints.empty() && point.timeNs < previous) return false;
                    previous = point.timeNs;
                    if (entry["cell"])
                    {
                        point.cell = entry["cell"].as<std::string>();
                        if (point.cell.size() > 256) return false;
                        point.hasCell = true;
                    }
                    if (entry["x"] || entry["y"] || entry["z"])
                    {
                        double x, y, z, tolerance;
                        if (!boundedNumber(entry, "x", -1e9, 1e9, x) || !boundedNumber(entry, "y", -1e9, 1e9, y)
                            || !boundedNumber(entry, "z", -1e9, 1e9, z)
                            || !boundedNumber(entry, "tolerance", 0.0, 1e6, tolerance)) return false;
                        point.x = x; point.y = y; point.z = z; point.tolerance = tolerance; point.hasPose = true;
                    }
                    if (entry["rot_x"] || entry["rot_y"] || entry["rot_z"])
                    {
                        double x, y, z, tolerance;
                        if (!boundedNumber(entry, "rot_x", -1000.0, 1000.0, x)
                            || !boundedNumber(entry, "rot_y", -1000.0, 1000.0, y)
                            || !boundedNumber(entry, "rot_z", -1000.0, 1000.0, z)
                            || !boundedNumber(entry, "rotation_tolerance", 0.0, 1000.0, tolerance)) return false;
                        point.rotX = x; point.rotY = y; point.rotZ = z; point.rotationTolerance = tolerance; point.hasRotation = true;
                    }
                    if (!point.hasCell && !point.hasPose && !point.hasRotation) return false;
                    parsed.checkpoints.push_back(std::move(point));
                }
            }
            scenario = std::move(parsed);
            return true;
        }
        catch (...) { return false; }
    }

    bool constantTimeEqual(std::string_view left, std::string_view right)
    {
        size_t difference = left.size() ^ right.size();
        const size_t count = std::max(left.size(), right.size());
        for (size_t i = 0; i < count; ++i)
            difference |= static_cast<size_t>((i < left.size() ? left[i] : 0) ^ (i < right.size() ? right[i] : 0));
        return difference == 0;
    }

    std::string jsonEscape(std::string_view value)
    {
        std::string out;
        out.reserve(value.size() + 8);
        for (unsigned char c : value)
        {
            switch (c)
            {
                case '"': out += "\\\""; break;
                case '\\': out += "\\\\"; break;
                case '\n': out += "\\n"; break;
                case '\r': out += "\\r"; break;
                case '\t': out += "\\t"; break;
                default: if (c < 0x20) out += '?'; else out += static_cast<char>(c);
            }
        }
        return out;
    }

    class Sha256
    {
        static const uint32_t* constants()
        {
            static const uint32_t values[64] = {
                0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
                0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
                0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
                0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
                0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
                0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
                0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
                0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
            };
            return values;
        }
        uint32_t mHash[8] = {0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
        uint8_t mBlock[64]{};
        uint64_t mLength = 0;
        size_t mUsed = 0;
        void transform(const uint8_t* block)
        {
            uint32_t w[64];
            for (unsigned i = 0; i < 16; ++i)
                w[i] = (uint32_t(block[i*4])<<24)|(uint32_t(block[i*4+1])<<16)|(uint32_t(block[i*4+2])<<8)|block[i*4+3];
            for (unsigned i = 16; i < 64; ++i)
            {
                const uint32_t s0 = ((w[i-15] >> 7) | (w[i-15] << 25)) ^ ((w[i-15] >> 18) | (w[i-15] << 14)) ^ (w[i-15] >> 3);
                const uint32_t s1 = ((w[i-2] >> 17) | (w[i-2] << 15)) ^ ((w[i-2] >> 19) | (w[i-2] << 13)) ^ (w[i-2] >> 10);
                w[i] = w[i-16] + s0 + w[i-7] + s1;
            }
            uint32_t a=mHash[0],b=mHash[1],c=mHash[2],d=mHash[3],e=mHash[4],f=mHash[5],wordG=mHash[6],h=mHash[7];
            const uint32_t* k = constants();
            for (unsigned i = 0; i < 64; ++i)
            {
                const uint32_t s1=((e>>6)|(e<<26))^((e>>11)|(e<<21))^((e>>25)|(e<<7));
                const uint32_t ch=(e&f)^((~e)&wordG);
                const uint32_t t1=h+s1+ch+k[i]+w[i];
                const uint32_t s0=((a>>2)|(a<<30))^((a>>13)|(a<<19))^((a>>22)|(a<<10));
                const uint32_t maj=(a&b)^(a&c)^(b&c);
                const uint32_t t2=s0+maj;
                h=wordG; wordG=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
            }
            mHash[0]+=a; mHash[1]+=b; mHash[2]+=c; mHash[3]+=d;
            mHash[4]+=e; mHash[5]+=f; mHash[6]+=wordG; mHash[7]+=h;
        }
    public:
        void update(const uint8_t* data, size_t size)
        {
            mLength += size;
            while (size)
            {
                const size_t count = std::min(size, sizeof(mBlock) - mUsed);
                std::memcpy(mBlock + mUsed, data, count);
                data += count; size -= count; mUsed += count;
                if (mUsed == sizeof(mBlock)) { transform(mBlock); mUsed = 0; }
            }
        }
        std::string finish()
        {
            const uint64_t bits = mLength * 8;
            mBlock[mUsed++] = 0x80;
            if (mUsed > 56)
            {
                std::memset(mBlock + mUsed, 0, sizeof(mBlock) - mUsed);
                transform(mBlock);
                mUsed = 0;
            }
            std::memset(mBlock + mUsed, 0, 56 - mUsed);
            for (unsigned i = 0; i < 8; ++i) mBlock[63-i] = static_cast<uint8_t>(bits >> (i*8));
            transform(mBlock);
            std::ostringstream out;
            out << std::hex << std::setfill('0');
            for (uint32_t word : mHash) out << std::setw(8) << word;
            return out.str();
        }
    };

    std::string sha256File(const std::filesystem::path& path)
    {
        std::ifstream input(path, std::ios::binary);
        if (!input) return {};
        Sha256 hash;
        std::array<uint8_t, 65536> chunk{};
        while (input)
        {
            input.read(reinterpret_cast<char*>(chunk.data()), static_cast<std::streamsize>(chunk.size()));
            const std::streamsize count = input.gcount();
            if (count > 0) hash.update(chunk.data(), static_cast<size_t>(count));
        }
        if (!input.eof()) return {};
        return hash.finish();
    }

    const char* captureName(CaptureState state)
    {
        switch (state)
        {
            case CaptureState::Idle: return "idle";
            case CaptureState::Loading: return "loading";
            case CaptureState::WaitingWorld: return "waiting_world";
            case CaptureState::Warming: return "warming";
            case CaptureState::Capturing: return "capturing";
            case CaptureState::Complete: return "complete";
            case CaptureState::Incomplete: return "incomplete";
            case CaptureState::Error: return "error";
        }
        return "error";
    }

    void publishStatus()
    {
        auto& status = g.published;
        const std::string_view loaded = g.scenarioLoaded ? std::string_view(g.loadedSave) : std::string_view();
        const int workerError = g.workerError.load();
        const std::string_view error = workerError == 1 ? "socket_create_failed"
            : workerError == 2 ? "socket_bind_failed" : std::string_view(g.lastError);
        if (status.ready == g.ready && status.capture == g.capture && status.completed == g.completed
            && status.gameState == g.gameState && status.runId == g.runId && status.manifest == g.manifestFile
            && status.frames == g.framesFile && status.loadedSave == loaded && status.error == error
            && status.lastCommandId == g.lastCommandId && status.lastCommandError == g.lastCommandError) return;
        std::lock_guard<std::mutex> lock(g.statusMutex);
        status.ready = g.ready;
        status.capture = g.capture;
        status.completed = g.completed;
        if (status.gameState != g.gameState) status.gameState = g.gameState;
        if (status.runId != g.runId) status.runId = g.runId;
        if (status.manifest != g.manifestFile) status.manifest = g.manifestFile;
        if (status.frames != g.framesFile) status.frames = g.framesFile;
        if (status.loadedSave != loaded)
        {
            if (loaded.empty()) status.loadedSave.clear();
            else status.loadedSave.assign(loaded.data(), loaded.size());
        }
        if (status.error != error)
        {
            if (error.empty()) status.error.clear();
            else status.error.assign(error.data(), error.size());
        }
        status.lastCommandId = g.lastCommandId;
        if (status.lastCommandError != g.lastCommandError) status.lastCommandError = g.lastCommandError;
    }

    std::string statusJson(uint64_t id, bool accepted, bool complete, std::string_view error = {})
    {
        std::lock_guard<std::mutex> lock(g.statusMutex);
        const auto& status = g.published;
        std::ostringstream out;
        out << "{\"version\":1,\"id\":" << id << ",\"accepted\":" << (accepted ? "true" : "false")
            << ",\"complete\":" << (complete ? "true" : "false");
        if (!error.empty()) out << ",\"error\":\"" << jsonEscape(error) << '"';
        out << ",\"status\":{\"ready\":" << (status.ready ? "true" : "false") << ",\"game_state\":\""
            << jsonEscape(status.gameState) << "\",\"run_id\":\"" << jsonEscape(status.runId) << "\",\"capture_state\":\""
            << captureName(status.capture) << "\",\"completed\":" << (status.completed ? "true" : "false")
            << ",\"manifest\":\"" << jsonEscape(status.manifest) << "\",\"frames\":\"" << jsonEscape(status.frames)
            << "\",\"loaded_save\":\"" << jsonEscape(status.loadedSave) << "\",\"error\":\""
            << jsonEscape(status.error) << "\",\"last_command_id\":" << status.lastCommandId
            << ",\"last_command_error\":\"" << jsonEscape(status.lastCommandError) << "\"}}";
        return out.str();
    }

    bool enqueue(const Command& command)
    {
        std::lock_guard<std::mutex> lock(g.queueMutex);
        if (g.queueSize == QueueCapacity) return false;
        g.queue[(g.queueHead + g.queueSize) % QueueCapacity] = command;
        ++g.queueSize;
        return true;
    }

    bool dequeue(Command& command)
    {
        std::lock_guard<std::mutex> lock(g.queueMutex);
        if (!g.queueSize) return false;
        command = std::move(g.queue[g.queueHead]);
        g.queue[g.queueHead] = Command{};
        g.queueHead = (g.queueHead + 1) % QueueCapacity;
        --g.queueSize;
        return true;
    }

    bool receiveLine(int socket, std::string& line)
    {
        line.clear();
        const auto deadline = Clock::now() + std::chrono::seconds(1);
        while (!g.stopWorker.load())
        {
            const auto remaining = deadline - Clock::now();
            if (remaining <= Clock::duration::zero()) return false;
            const auto micros = std::chrono::duration_cast<std::chrono::microseconds>(remaining).count();
            timeval timeout{static_cast<time_t>(micros / 1000000), static_cast<suseconds_t>(micros % 1000000)};
            fd_set readSet;
            FD_ZERO(&readSet); FD_SET(socket, &readSet);
            const int ready = select(socket + 1, &readSet, nullptr, nullptr, &timeout);
            if (ready <= 0) return false;
            char c;
            const ssize_t received = recv(socket, &c, 1, 0);
            if (received == 0) return false;
            if (received < 0)
            {
                if (errno == EINTR) continue;
                return false;
            }
            if (c == '\n') return true;
            if (line.size() == ProtocolLineBytes) return false;
            if (c != '\r') line.push_back(c);
        }
        return false;
    }

    bool sendLine(int socket, const std::string& line)
    {
        std::string framed = line + "\n";
        size_t offset = 0;
        while (offset < framed.size())
        {
            int flags = 0;
#ifdef MSG_NOSIGNAL
            flags = MSG_NOSIGNAL;
#endif
            const ssize_t sent = send(socket, framed.data() + offset, framed.size() - offset, flags);
            if (sent <= 0) return false;
            offset += static_cast<size_t>(sent);
        }
        return true;
    }

    void closeSocket(int socket)
    {
        ::shutdown(socket, SHUT_RDWR);
        ::close(socket);
    }

    void workerMain()
    {
        const int listener = socket(AF_INET, SOCK_STREAM, 0);
        if (listener < 0) { g.workerError.store(1); return; }
        int reuse = 1;
        setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_port = htons(g.config.port);
        if (inet_pton(AF_INET, g.config.bind.c_str(), &address.sin_addr) != 1 || bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0
            || listen(listener, 1) < 0)
        {
            closeSocket(listener);
            g.workerError.store(2);
            return;
        }
        g.listenSocket.store(listener);
        timeval timeout{0, 200000};
        setsockopt(listener, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
        while (!g.stopWorker.load())
        {
            fd_set readSet;
            FD_ZERO(&readSet); FD_SET(listener, &readSet);
            timeval wait{0, 200000};
            if (select(listener + 1, &readSet, nullptr, nullptr, &wait) <= 0) continue;
            const int client = accept(listener, nullptr, nullptr);
            if (client < 0) continue;
#ifdef SO_NOSIGPIPE
            int noSigPipe = 1;
            setsockopt(client, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe, sizeof(noSigPipe));
#endif
            timeval clientTimeout{1, 0};
            setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &clientTimeout, sizeof(clientTimeout));
            setsockopt(client, SOL_SOCKET, SO_SNDTIMEO, &clientTimeout, sizeof(clientTimeout));
            bool authenticated = false;
            std::string line;
            while (!g.stopWorker.load() && receiveLine(client, line))
            {
                uint64_t id = 0;
                try
                {
                    if (!JsonValidator(line).valid()) throw std::runtime_error("invalid_request");
                    YAML::Node request = YAML::Load(line);
                    if (!request.IsMap() || !request["version"] || request["version"].as<int>() != 1 || !request["id"]
                        || !request["id"].IsScalar() || !request["token"] || !request["command"])
                        throw std::runtime_error("invalid_request");
                    id = request["id"].as<uint64_t>();
                    if (id == 0) throw std::runtime_error("invalid_id");
                    const std::string token = request["token"].as<std::string>();
                    if (!constantTimeEqual(token, g.config.token)) throw std::runtime_error("authentication_failed");
                    authenticated = true;
                    const std::string name = request["command"].as<std::string>();
                    if (name == "status")
                    {
                        if (!sendLine(client, statusJson(id, true, true))) break;
                        continue;
                    }
                    Command command;
                    command.id = id;
                    if (name == "load_save")
                    {
                        command.type = CommandType::LoadSave;
                        if (!request["save"]) throw std::runtime_error("missing_save");
                        command.name = request["save"].as<std::string>();
                        if (!safeFilename(command.name)) throw std::runtime_error("invalid_save");
                    }
                    else if (name == "run_scenario")
                    {
                        command.type = CommandType::RunScenario;
                        if (!request["scenario"] || !request["run_id"]) throw std::runtime_error("missing_scenario_or_run_id");
                        command.name = request["scenario"].as<std::string>();
                        if (!safeName(command.name)) throw std::runtime_error("invalid_scenario");
                        const std::string runId = request["run_id"].as<std::string>();
                        if (!safeName(runId)) throw std::runtime_error("invalid_run_id");
                        command.name += '\n'; command.name += runId;
                    }
                    else if (name == "stop_capture") command.type = CommandType::Stop;
                    else if (name == "quit") command.type = CommandType::Quit;
                    else if (name == "input_state")
                    {
                        command.type = CommandType::Input;
                        if (!request["input"] || !parseInput(request["input"], command.input)
                            || !boundedNumber(request, "lease_ms", 1u, 1000u, command.leaseMs)) throw std::runtime_error("invalid_input_state");
                    }
                    else throw std::runtime_error("unknown_command");
                    const char* transitionError = nullptr;
                    {
                        std::lock_guard<std::mutex> lock(g.statusMutex);
                        const CaptureState current = g.published.capture;
                        const bool active = current == CaptureState::Loading || current == CaptureState::WaitingWorld
                            || current == CaptureState::Warming || current == CaptureState::Capturing;
                        if (command.type == CommandType::LoadSave && active) transitionError = "run_or_load_active";
                        else if (command.type == CommandType::RunScenario
                            && (!g.published.ready || g.published.loadedSave.empty() || active)) transitionError = "save_not_ready";
                        else if (command.type == CommandType::Stop
                            && current != CaptureState::Warming && current != CaptureState::Capturing) transitionError = "no_active_run";
                    }
                    if (transitionError)
                    {
                        if (!sendLine(client, statusJson(id, false, true, transitionError))) break;
                        continue;
                    }
                    if (!enqueue(command))
                    {
                        if (!sendLine(client, statusJson(id, false, true, "command_queue_full"))) break;
                    }
                    else if (!sendLine(client, statusJson(id, true, false))) break;
                }
                catch (const std::exception& e)
                {
                    const std::string message = authenticated ? e.what() : "authentication_failed";
                    if (!sendLine(client, statusJson(id, false, true, message))) break;
                }
            }
            closeSocket(client);
            if (authenticated)
            {
                g.disconnectPending.store(true, std::memory_order_release);
                while (!g.stopWorker.load() && g.disconnectPending.load(std::memory_order_acquire))
                    std::this_thread::sleep_for(std::chrono::milliseconds(1));
            }
        }
        g.listenSocket.store(-1);
        closeSocket(listener);
    }


    void finalizeCapture(const char* status, const char* reason)
    {
        if (g.outputFinalized) return;
        g.outputFinalized = true;
        const uint64_t ended = nowNs();
        g.capture = std::string_view(status) == "complete" ? CaptureState::Complete : CaptureState::Incomplete;
        g.lastError = reason ? reason : "";
        g.completed = false;
        const auto runDir = g.workspace / g.runPath;
        std::error_code ec;
        std::filesystem::create_directories(runDir, ec);
        if (ec) { g.capture = CaptureState::Error; g.lastError = "output_directory_failed"; return; }
        const std::filesystem::path framesPath = runDir / "frames.csv";
        std::ofstream csv(framesPath, std::ios::binary | std::ios::trunc);
        if (!csv) { g.capture = CaptureState::Error; g.lastError = "frames_open_failed"; return; }
        csv << "frame_id,phase,scenario_time_ns,outer_interval_ns,update_ns,render_ns,lua_wait_ns,limiter_ns,checkpoint_index,paused,loading,cell_id,player_x,player_y,player_z,player_rot_x,player_rot_y,player_rot_z\n";
        for (const FrameRecord& record : g.frames)
        {
            csv << record.frameId << ",capture," << record.scenarioTimeNs << ',' << record.outerNs
                << ',' << record.updateNs << ',' << record.renderNs << ',' << record.luaWaitNs << ',' << record.limiterNs << ','
                << record.checkpoint << ',' << unsigned(record.paused) << ',' << unsigned(record.loading) << ',' << record.cellId << ','
                << std::setprecision(9) << record.x << ',' << record.y << ',' << record.z << ','
                << record.rotX << ',' << record.rotY << ',' << record.rotZ << '\n';
        }
        csv.flush();
        if (!csv) { g.capture = CaptureState::Error; g.lastError = "frames_write_failed"; return; }
        csv.close();
        if (!csv) { g.capture = CaptureState::Error; g.lastError = "frames_write_failed"; return; }
        const std::string framesHash = sha256File(framesPath);
        if (framesHash.empty())
        {
            g.capture = CaptureState::Error;
            g.lastError = "frames_hash_read_failed";
            publishStatus();
            return;
        }
        const std::filesystem::path manifestPath = runDir / "manifest.json";
        std::ofstream manifest(manifestPath, std::ios::binary | std::ios::trunc);
        if (!manifest) { g.capture = CaptureState::Error; g.lastError = "manifest_open_failed"; return; }
        manifest << "{\"schema_version\":1,\"run_id\":\"" << jsonEscape(g.runId) << "\",\"status\":\""
            << (g.capture == CaptureState::Complete ? "complete" : "incomplete") << "\",\"frame_count\":" << g.frames.size()
            << ",\"frame_capacity\":" << g.config.maxFrames << ",\"reason\":\"" << jsonEscape(g.lastError)
            << "\",\"started_at_ns\":" << g.runStartedNs << ",\"ended_at_ns\":" << ended
            << ",\"duration_ns\":" << (ended >= g.runStartedNs ? ended - g.runStartedNs : 0)
            << ",\"frames_file\":\"frames.csv\",\"frames_sha256\":\"" << framesHash << "\"}\n";
        manifest.flush();
        if (!manifest) { g.capture = CaptureState::Error; g.lastError = "manifest_write_failed"; return; }
        manifest.close();
        if (!manifest) { g.capture = CaptureState::Error; g.lastError = "manifest_write_failed"; return; }
        g.framesFile = "frames.csv";
        g.manifestFile = "runs/" + g.runId + "/manifest.json";
        g.completed = g.capture == CaptureState::Complete;
    }

    bool containedRegularSave(const std::string& save)
    {
        // Names are validated before queuing or parsing a scenario. PS5 denies
        // realpath in the title sandbox, so inspect entries without following links.
        std::error_code ec;
        const auto root = g.workspace / "saves";
        const auto rootStatus = std::filesystem::symlink_status(root, ec);
        if (ec || !std::filesystem::is_directory(rootStatus)) return false;
        const auto candidateStatus = std::filesystem::symlink_status(root / save, ec);
        return !ec && std::filesystem::is_regular_file(candidateStatus);
    }

    void clearInput();
    bool beginScenario(const std::string& filename, const std::string& runId, bool loadSave, bool networkControlled)
    {
        if (!g.config.captureEnabled || !parseScenario(filename, g.scenario)) return false;
        if (g.capture == CaptureState::Capturing || g.capture == CaptureState::Warming
            || g.capture == CaptureState::Loading || g.capture == CaptureState::WaitingWorld) return false;
        if (!containedRegularSave(g.scenario.save)) return false;
        if (!loadSave && (!g.scenarioLoaded || g.loadedSave != g.scenario.save)) return false;
        if (loadSave && !g.callbacks.loadSave) return false;
        g.runId = runId;
        g.runPath = "runs/" + runId;
        std::error_code ec;
        if (std::filesystem::exists(g.workspace / g.runPath, ec) || ec) return false;
        std::filesystem::create_directories(g.workspace / "runs", ec);
        if (ec) return false;
        clearInput();
        g.frames.clear();
        g.frames.reserve(g.config.maxFrames);
        g.runStartedNs = nowNs();
        g.measuredElapsedNs = 0;
        g.scenarioTimeNs = 0;
        g.nextEvent = g.nextCheckpoint = 0;
        g.activeCheckpoint = -1;
        g.outputFinalized = false;
        g.runInvalid = false;
        g.completed = false;
        g.lastError.clear();
        g.manifestFile.clear();
        g.framesFile.clear();
        g.pendingRecordValid = false;
        g.scenarioLoaded = !loadSave;
        g.capture = loadSave ? CaptureState::Loading : CaptureState::Warming;
        g.gameState = loadSave ? "loading" : "ready";
        g.networkRun = networkControlled;
        if (loadSave)
        {
            g.loadedSave = g.scenario.save;
            g.loadAwaitingReady = true;
            g.loadSawNotReady = true;
            try { g.callbacks.loadSave(g.scenario.save); }
            catch (...) { g.capture = CaptureState::Error; g.loadAwaitingReady = false; g.lastError = "load_callback_failed"; return false; }
        }
        else
        {
            g.simulationOrigin = g.simulationNow;
            g.warmupStartNs = nowNs();
        }
        return true;
    }

    void publishInput()
    {
        std::lock_guard<std::mutex> lock(g.inputMutex);
        g.inputSnapshot = g.currentInput;
        g.inputSnapshotActive.store(g.inputIsActive, std::memory_order_release);
    }

    void clearInput()
    {
        g.inputIsActive = false;
        g.inputIsManual = false;
        g.currentInput = InputState{};
        publishInput();
    }

    void failRun(const char* reason)
    {
        g.runInvalid = true;
        g.lastError = reason;
        clearInput();
        if (g.capture == CaptureState::Capturing || g.capture == CaptureState::Warming)
            finalizeCapture("incomplete", reason);
        else g.capture = CaptureState::Error;
    }

    void startCapture()
    {
        if (g.capture != CaptureState::Warming) return;
        g.capture = CaptureState::Capturing;
        g.captureStartedNs = nowNs();
        g.runStartedNs = g.captureStartedNs;
        g.measuredElapsedNs = 0;
        g.pendingRecordValid = false;
    }

    void processCommand(const Command& command, const GameStateSnapshot& snapshot)
    {
        if (command.id)
        {
            g.lastCommandId = command.id;
            g.lastCommandError.clear();
            g.lastError.clear();
        }
        auto reject = [&](const char* reason)
        {
            g.lastError = reason;
            g.lastCommandError = reason;
        };
        switch (command.type)
        {
            case CommandType::LoadSave:
                if (g.capture == CaptureState::Capturing || g.capture == CaptureState::Warming
                    || g.capture == CaptureState::Loading || g.capture == CaptureState::WaitingWorld)
                { reject("run_or_load_active"); break; }
                if (!g.callbacks.loadSave || !containedRegularSave(command.name))
                { reject("save_not_found"); break; }
                clearInput();
                g.capture = CaptureState::Loading;
                g.gameState = "loading";
                g.ready = false;
                g.scenarioLoaded = false;
                g.loadedSave = command.name;
                g.loadAwaitingReady = true;
                g.loadSawNotReady = true;
                try { g.callbacks.loadSave(command.name); }
                catch (...) { g.capture = CaptureState::Error; g.loadAwaitingReady = false; reject("load_callback_failed"); }
                break;
            case CommandType::RunScenario:
            {
                const size_t split = command.name.find('\n');
                if (split == std::string::npos) { reject("invalid_run_request"); g.capture = CaptureState::Error; break; }
                if (g.capture == CaptureState::Capturing || g.capture == CaptureState::Warming
                    || g.capture == CaptureState::Loading || g.capture == CaptureState::WaitingWorld)
                { reject("run_active"); break; }
                const std::string scenarioName = command.name.substr(0, split);
                const std::string requestedRunId = command.name.substr(split + 1);
                g.runId = requestedRunId;
                g.completed = false;
                g.manifestFile.clear();
                g.framesFile.clear();
                g.capture = CaptureState::Error;
                if (!snapshot.ready || !g.scenarioLoaded)
                { reject("save_not_ready"); break; }
                Scenario parsed;
                if (!parseScenario(scenarioName, parsed) || parsed.save != g.loadedSave)
                { reject("scenario_save_mismatch"); break; }
                if (!beginScenario(scenarioName, requestedRunId, false, true))
                    reject("scenario_rejected");
                break;
            }
            case CommandType::Stop:
                if (g.capture == CaptureState::Capturing || g.capture == CaptureState::Warming)
                {
                    clearInput();
                    finalizeCapture("incomplete", "stopped_by_command");
                }
                else reject("no_active_run");
                break;
            case CommandType::Input:
                if (g.capture == CaptureState::Capturing || g.capture == CaptureState::Warming)
                { failRun("manual_input_interrupted_run"); reject("manual_input_interrupted_run"); break; }
                g.currentInput = command.input;
                g.inputIsActive = true;
                g.inputIsManual = true;
                g.leaseDeadline = Clock::now() + std::chrono::milliseconds(command.leaseMs);
                publishInput();
                break;
            case CommandType::Quit:
                clearInput();
                if (g.capture == CaptureState::Capturing || g.capture == CaptureState::Warming)
                    finalizeCapture("incomplete", "quit_during_run");
                if (g.callbacks.requestQuit)
                {
                    try { g.callbacks.requestQuit(); }
                    catch (...) { reject("quit_callback_failed"); }
                }
                break;
        }
    }

    void closePendingFrame(uint64_t now)
    {
        if (!g.pendingRecordValid || g.capture != CaptureState::Capturing) return;
        if (g.frames.size() >= g.config.maxFrames)
        {
            g.pendingRecordValid = false;
            failRun("frame_buffer_overflow");
            return;
        }
        g.pendingRecord.outerNs = now >= g.pendingStartNs ? now - g.pendingStartNs : 0;
        g.frames.push_back(g.pendingRecord);
        g.measuredElapsedNs += g.pendingRecord.outerNs;
        g.pendingRecordValid = false;
        if (g.measuredElapsedNs >= g.scenario.durationNs)
        {
            clearInput();
            finalizeCapture("complete", "");
        }
    }

    void workerStart()
    {
        if (g.config.controlEnabled && !g.worker.joinable())
        {
            g.stopWorker = false;
            g.worker = std::thread(workerMain);
        }
    }
}

void initialize(const std::filesystem::path& configFile, Callbacks callbacks)
{
    if (g.started) return;
    unsetenv("OPENMW_PS5_BENCHMARK_ISOLATED");
    g.workspace =
#if defined(__PROSPERO__)
        "/download0/benchmark";
#else
        configFile.parent_path() / "benchmark";
#endif
    g.callbacks = std::move(callbacks);
    if (!parseConfig(configFile, g.config))
    {
        g.lastError = g.config.error;
        return;
    }
    if (!g.config.enabled) return;
    std::error_code ec;
    std::filesystem::create_directories(g.workspace / "config", ec);
    if (!ec) std::filesystem::create_directories(g.workspace / "user", ec);
    if (!ec) std::filesystem::create_directories(g.workspace / "cache", ec);
    if (!ec) std::filesystem::create_directories(g.workspace / "saves", ec);
    if (!ec) std::filesystem::create_directories(g.workspace / "scenarios", ec);
    if (!ec) std::filesystem::create_directories(g.workspace / "runs", ec);
    if (ec)
    {
        g.config.enabled = false;
        g.lastError = "workspace_create_failed";
        return;
    }
    if (setenv("OPENMW_PS5_BENCHMARK_ISOLATED", "1", 1) != 0)
    {
        g.config.enabled = false;
        g.lastError = "isolation_flag_failed";
        return;
    }
    g.started = true;
    publishStatus();
    workerStart();
}

#if !defined(__PROSPERO__)
void initialize(const std::filesystem::path& configFile, Callbacks callbacks, const std::filesystem::path& workspaceRoot)
{
    if (g.started) return;
    unsetenv("OPENMW_PS5_BENCHMARK_ISOLATED");
    g.workspace = workspaceRoot;
    g.callbacks = std::move(callbacks);
    if (!parseConfig(configFile, g.config))
    {
        g.lastError = g.config.error;
        return;
    }
    if (!g.config.enabled) return;
    std::error_code ec;
    std::filesystem::create_directories(g.workspace / "config", ec);
    if (!ec) std::filesystem::create_directories(g.workspace / "user", ec);
    if (!ec) std::filesystem::create_directories(g.workspace / "cache", ec);
    if (!ec) std::filesystem::create_directories(g.workspace / "saves", ec);
    if (!ec) std::filesystem::create_directories(g.workspace / "scenarios", ec);
    if (!ec) std::filesystem::create_directories(g.workspace / "runs", ec);
    if (ec) { g.config.enabled = false; g.lastError = "workspace_create_failed"; return; }
    if (setenv("OPENMW_PS5_BENCHMARK_ISOLATED", "1", 1) != 0)
    { g.config.enabled = false; g.lastError = "isolation_flag_failed"; return; }
    g.started = true;
    publishStatus();
    workerStart();
}
#endif

void bindCallbacks(Callbacks callbacks)
{
    if (!g.started) return;
    g.callbacks = std::move(callbacks);
}

bool enabled() { return g.config.enabled; }
bool controlEnabled() { return g.config.enabled && g.config.controlEnabled; }
bool captureEnabled() { return g.config.enabled && g.config.captureEnabled; }
bool inputActive() { return g.config.enabled && g.inputSnapshotActive.load(std::memory_order_acquire); }

void applyIsolatedPaths(std::filesystem::path& config, std::filesystem::path& user, std::filesystem::path& cache)
{
    if (!g.config.enabled) return;
    config = g.workspace / "config";
    user = g.workspace / "user";
    cache = g.workspace / "cache";
}

void gameThreadPump(const GameStateSnapshot& snapshot)
{
    if (!g.config.enabled) return;
    g.haveSnapshot = true;
    g.ready = snapshot.ready;
    g.loading = snapshot.loading;
    g.paused = snapshot.paused;
    g.playerX = snapshot.playerX;
    g.playerY = snapshot.playerY;
    g.playerZ = snapshot.playerZ;
    g.playerRotX = snapshot.playerRotX;
    g.playerRotY = snapshot.playerRotY;
    g.playerRotZ = snapshot.playerRotZ;
    g.simulationNow = snapshot.simulationSeconds;
    const char* gameState = snapshot.loading ? "loading" : snapshot.ready ? (snapshot.paused ? "paused" : "ready") : "menu";
    if (g.gameState != gameState) g.gameState = gameState;
    if (g.config.captureEnabled && g.cellName != snapshot.cellName)
    {
        if (snapshot.cellName.empty()) g.cellName.clear();
        else g.cellName.assign(snapshot.cellName.data(), snapshot.cellName.size());
        ++g.cellId;
    }

    if (g.loadAwaitingReady)
    {
        if (!snapshot.ready || snapshot.loading)
        {
            g.loadSawNotReady = true;
            g.ready = false;
            g.gameState = "loading";
        }
        else if (g.loadSawNotReady)
        {
            g.loadAwaitingReady = false;
            g.scenarioLoaded = true;
            if (g.standalonePending)
            {
                g.standalonePending = false;
                g.capture = CaptureState::Warming;
                g.simulationOrigin = snapshot.simulationSeconds;
                g.warmupStartNs = nowNs();
                g.scenarioTimeNs = 0;
            }
            else g.capture = CaptureState::Idle;
        }
    }

    if (!g.config.startupScenario.empty() && !g.startupAttempted)
    {
        g.startupAttempted = true;
        g.standalonePending = true;
        const auto wallNs = std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count();
        std::string startupRunId;
        std::error_code runIdError;
        for (uint32_t suffix = 0; suffix < 10000; ++suffix)
        {
            startupRunId = "startup-" + std::to_string(wallNs);
            if (suffix) startupRunId += "-" + std::to_string(suffix);
            if (!std::filesystem::exists(g.workspace / "runs" / startupRunId, runIdError) && !runIdError) break;
            if (runIdError) { startupRunId.clear(); break; }
            startupRunId.clear();
        }
        if (startupRunId.empty() || !beginScenario(g.config.startupScenario, startupRunId, true, false))
        {
            g.capture = CaptureState::Error;
            g.lastError = "startup_scenario_rejected";
            g.standalonePending = false;
        }
    }

    if (g.capture == CaptureState::Warming || g.capture == CaptureState::Capturing)
    {
        if (!snapshot.ready || snapshot.loading || snapshot.paused)
        {
            failRun(snapshot.paused ? "paused_during_run" : "world_not_ready_during_run");
        }
        else
        {
            const double elapsedSimulation = snapshot.simulationSeconds - g.simulationOrigin;
            if (!std::isfinite(elapsedSimulation) || elapsedSimulation < 0.0)
                failRun("invalid_simulation_clock");
            else
                g.scenarioTimeNs = static_cast<uint64_t>(elapsedSimulation * 1e9);

            bool inputChanged = false;
            while (g.capture != CaptureState::Error && g.capture != CaptureState::Incomplete
                && g.nextEvent < g.scenario.events.size() && g.scenario.events[g.nextEvent].timeNs <= g.scenarioTimeNs)
            {
                g.currentInput = g.scenario.events[g.nextEvent++].input;
                g.inputIsActive = true;
                inputChanged = true;
            }
            if (g.capture == CaptureState::Warming && !g.inputIsActive)
            {
                g.currentInput = InputState{};
                g.inputIsActive = true;
                inputChanged = true;
            }
            if (inputChanged) publishInput();
            if (g.capture == CaptureState::Warming || g.capture == CaptureState::Capturing)
            {
                while (g.nextCheckpoint < g.scenario.checkpoints.size()
                    && g.scenario.checkpoints[g.nextCheckpoint].timeNs <= g.scenarioTimeNs)
                {
                    g.activeCheckpoint = static_cast<int32_t>(g.nextCheckpoint);
                    const Checkpoint& point = g.scenario.checkpoints[g.nextCheckpoint++];
                    const double dx = snapshot.playerX - point.x;
                    const double dy = snapshot.playerY - point.y;
                    const double dz = snapshot.playerZ - point.z;
                    const double drx = snapshot.playerRotX - point.rotX;
                    const double dry = snapshot.playerRotY - point.rotY;
                    const double drz = snapshot.playerRotZ - point.rotZ;
                    if ((point.hasCell && point.cell != g.cellName)
                        || (point.hasPose && std::sqrt(dx*dx + dy*dy + dz*dz) > point.tolerance)
                        || (point.hasRotation && std::sqrt(drx*drx + dry*dry + drz*drz) > point.rotationTolerance))
                    {
                        failRun("checkpoint_diverged");
                        break;
                    }
                }
            }
            if (g.capture == CaptureState::Warming && nowNs() - g.warmupStartNs >= g.scenario.warmupNs)
                startCapture();
        }
    }
    if (g.inputIsManual && Clock::now() >= g.leaseDeadline) clearInput();
    Command command;
    size_t commandBudget = QueueCapacity;
    while (commandBudget-- && dequeue(command)) processCommand(command, snapshot);
    if (g.disconnectPending.exchange(false, std::memory_order_acq_rel))
    {
        if (g.inputIsManual) clearInput();
        if (g.networkRun && (g.capture == CaptureState::Capturing || g.capture == CaptureState::Warming))
            failRun("control_disconnected");
    }
    publishStatus();
}


void frameStart()
{
    if (!g.config.enabled || !g.config.captureEnabled) return;
    const uint64_t now = nowNs();
    closePendingFrame(now);
    g.frameStartNs = now;
    publishStatus();
}

void frameEnd(uint64_t updateNs, uint64_t renderNs, uint64_t luaWaitNs, uint64_t limiterNs)
{
    if (!g.config.enabled || !g.config.captureEnabled || g.capture != CaptureState::Capturing || !g.frameStartNs) return;
    FrameRecord record;
    record.frameId = g.frames.size() + 1;
    record.phase = 1;
    record.scenarioTimeNs = g.scenarioTimeNs;
    record.updateNs = updateNs;
    record.renderNs = renderNs;
    record.luaWaitNs = luaWaitNs;
    record.limiterNs = limiterNs;
    record.checkpoint = g.activeCheckpoint;
    record.cellId = g.cellId;
    record.x = g.playerX;
    record.y = g.playerY;
    record.z = g.playerZ;
    record.rotX = g.playerRotX;
    record.rotY = g.playerRotY;
    record.rotZ = g.playerRotZ;
    record.paused = g.paused;
    record.loading = g.loading;
    g.pendingRecord = record;
    g.pendingStartNs = std::max(g.frameStartNs, g.captureStartedNs);
    g.pendingRecordValid = true;
}

void frameSkipped()
{
    if (!g.config.enabled || !g.config.captureEnabled) return;
    g.pendingRecordValid = false;
    if (g.capture == CaptureState::Capturing || g.capture == CaptureState::Warming)
        failRun("outer_frame_skipped");
    publishStatus();
}

void physicalInputObserved()
{
    if (!g.config.enabled) return;
    if (g.capture == CaptureState::Capturing || g.capture == CaptureState::Warming) failRun("physical_input_interrupted_run");
    else if (g.inputIsManual) clearInput();
    publishStatus();

}
InputState inputState()
{
    if (!g.config.enabled) return InputState{};
    std::lock_guard<std::mutex> lock(g.inputMutex);
    return g.inputSnapshotActive.load(std::memory_order_relaxed) ? g.inputSnapshot : InputState{};
}

bool actionIsActive(Action action)
{
    const InputState input = inputState();
    switch (action)
    {
        case Action::Forward: return input.forward > 0.f;
        case Action::Backward: return input.forward < 0.f;
        case Action::Left: return input.strafe < 0.f;
        case Action::Right: return input.strafe > 0.f;
        case Action::Jump: return input.jump;
        case Action::Activate: return input.activate;
        case Action::Attack: return input.attack;
        case Action::Run: return input.run;
        case Action::Sneak: return input.sneak;
        case Action::LookX: return false;
        case Action::LookY: return false;
    }
    return false;
}

float actionValue(Action action)
{
    const InputState input = inputState();
    if (!inputActive() && action != Action::LookX && action != Action::LookY) return 0.f;
    switch (action)
    {
        case Action::Forward: return input.forward > 0.f ? 1.f : 0.f;
        case Action::Backward: return input.forward < 0.f ? 1.f : 0.f;
        case Action::Left: return input.strafe < 0.f ? 1.f : 0.f;
        case Action::Right: return input.strafe > 0.f ? 1.f : 0.f;
        case Action::LookX: return 0.5f + input.lookX * 0.5f;
        case Action::LookY: return 0.5f + input.lookY * 0.5f;
        case Action::Jump: return input.jump ? 1.f : 0.f;
        case Action::Activate: return input.activate ? 1.f : 0.f;
        case Action::Attack: return input.attack ? 1.f : 0.f;
        case Action::Run: return input.run ? 1.f : 0.f;
        case Action::Sneak: return input.sneak ? 1.f : 0.f;
    }
    return 0.5f;
}

void shutdown()
{
    if (!g.started) return;
    clearInput();
    if (g.capture == CaptureState::Capturing || g.capture == CaptureState::Warming)
        finalizeCapture("incomplete", "shutdown_during_run");
    g.stopWorker = true;
    if (g.worker.joinable()) g.worker.join();
    g.listenSocket.store(-1);
    g.callbacks = Callbacks{};
    g.started = false;
}
}
