#ifndef OPENMW_PS5BENCHMARK_HPP
#define OPENMW_PS5BENCHMARK_HPP

#include <array>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <string>
#include <string_view>

namespace MWPS5Benchmark
{
    struct InputState
    {
        float forward = 0.f;
        float strafe = 0.f;
        float lookX = 0.f;
        float lookY = 0.f;
        bool jump = false;
        bool activate = false;
        bool attack = false;
        bool run = false;
        bool sneak = false;
    };

    enum class Action
    {
        Forward,
        Backward,
        Left,
        Right,
        LookX,
        LookY,
        Jump,
        Activate,
        Attack,
        Run,
        Sneak
    };

    struct GameStateSnapshot
    {
        bool ready = false;
        bool loading = false;
        bool paused = false;
        std::string_view cellName;
        float playerX = 0.f;
        float playerY = 0.f;
        float playerZ = 0.f;
        float playerRotX = 0.f;
        float playerRotY = 0.f;
        float playerRotZ = 0.f;
        double simulationSeconds = 0.0;
    };

    struct Callbacks
    {
        std::function<void(std::string_view)> loadSave;
        std::function<void()> requestQuit;
    };

    // Config is read once during startup. Missing or disabled config is inert.
    void bindCallbacks(Callbacks callbacks);

    void initialize(const std::filesystem::path& configFile, Callbacks callbacks);
#if !defined(__PROSPERO__)
    // Host-runtime harness override. The network protocol cannot change this root.
    void initialize(const std::filesystem::path& configFile, Callbacks callbacks,
        const std::filesystem::path& workspaceRoot);
#endif
    bool enabled();
    bool controlEnabled();
    bool captureEnabled();
    bool inputActive();
    void applyIsolatedPaths(std::filesystem::path& config, std::filesystem::path& user,
        std::filesystem::path& cache);

    // Call only on the engine/game thread. Inputs are consumed by engine action getters.
    void gameThreadPump(const GameStateSnapshot& snapshot);
    void frameStart();
    void frameEnd(uint64_t updateNs, uint64_t renderNs, uint64_t luaWaitNs, uint64_t limiterNs);
    void frameSkipped();
    void physicalInputObserved();
    InputState inputState();
    bool actionIsActive(Action action);
    float actionValue(Action action);
    void shutdown();
}

#endif
