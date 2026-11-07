#ifndef OPENMW_COMPONENTS_FILES_PS5PATH_HPP
#define OPENMW_COMPONENTS_FILES_PS5PATH_HPP

#include <cstdlib>
#include <filesystem>
#include <string>
#include <string_view>
#include <vector>

namespace Files
{
    inline bool ps5BenchmarkIsolated()
    {
        const char* enabled = std::getenv("OPENMW_PS5_BENCHMARK_ISOLATED");
        return enabled != nullptr && std::string_view(enabled) == "1";
    }

    // Packaged assets live on M.2; benchmark writes stay separate from normal user data.
    struct Ps5Path
    {
        explicit Ps5Path(const std::string&) {}
        std::filesystem::path getUserConfigPath() const
        {
            return ps5BenchmarkIsolated() ? "/download0/benchmark/config/" : "/download0/config/";
        }
        std::filesystem::path getUserDataPath() const
        {
            return ps5BenchmarkIsolated() ? "/download0/benchmark/user/" : "/download0/user/";
        }
        std::filesystem::path getCachePath() const
        {
            return ps5BenchmarkIsolated() ? "/download0/benchmark/cache/" : "/download0/cache/";
        }
        std::filesystem::path getGlobalConfigPath() const { return "/app0/assets/"; }
        std::filesystem::path getLocalPath() const { return "/app0/assets/"; }
        std::filesystem::path getGlobalDataPath() const { return "/app0/assets/"; }
        std::vector<std::filesystem::path> getInstallPaths() const { return {}; }
    };
}

#endif
