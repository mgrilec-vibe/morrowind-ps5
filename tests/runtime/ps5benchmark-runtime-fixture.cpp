#include "ps5benchmark.hpp"

#include <chrono>
#include <filesystem>
#include <iostream>
#include <string>
#include <thread>

int main(int argc, char** argv)
{
    if (argc < 2 || argc > 3) return 2;
    using namespace MWPS5Benchmark;
    using Clock = std::chrono::steady_clock;
    const std::filesystem::path root(argv[1]);
    const std::string mode = argc == 3 ? argv[2] : "";
    bool quit = false;
    bool ready = false;
    bool markerObserved = false;
    bool sawScenarioInputAfterLease = false;
    Clock::time_point markerTime{};
    Callbacks callbacks;
    callbacks.loadSave = [&](std::string_view) { ready = true; };
    callbacks.requestQuit = [&] { quit = true; };
    initialize(root / "benchmark.json", callbacks, root);
    const auto deadline = Clock::now() + std::chrono::seconds(60);
    double simulationSeconds = 0;
    const auto nanos = [](auto start, auto end) {
        return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count());
    };
    while (!quit && Clock::now() < deadline)
    {
        const auto loopNow = Clock::now();
        if (mode == "warmup_checkpoint" && !markerObserved
            && std::filesystem::exists(root / "checkpoint-run-start"))
        {
            markerObserved = true;
            markerTime = loopNow;
        }
        if (mode == "manual_lease" && !markerObserved
            && std::filesystem::exists(root / "manual-run-start"))
        {
            markerObserved = true;
            markerTime = loopNow;
        }
        frameStart();
        GameStateSnapshot state;
        state.ready = ready;
        state.cellName = "Host runtime fixture";
        state.simulationSeconds = simulationSeconds;
        state.playerX = mode == "warmup_checkpoint" && markerObserved
                && loopNow - markerTime >= std::chrono::milliseconds(400) ? 99.f : 11.f;
        state.playerY = 22.f;
        state.playerZ = 33.f;
        gameThreadPump(state);
        if (mode == "manual_lease" && markerObserved
            && loopNow - markerTime >= std::chrono::milliseconds(200)
            && actionValue(Action::Forward) > 0.5f)
            sawScenarioInputAfterLease = true;
        const auto start = Clock::now();
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
        const auto updateEnd = Clock::now();
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
        const auto renderEnd = Clock::now();
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
        const auto luaEnd = Clock::now();
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
        const auto end = Clock::now();
        frameEnd(nanos(start, updateEnd), nanos(updateEnd, renderEnd), nanos(renderEnd, luaEnd), nanos(luaEnd, end));
        simulationSeconds += std::chrono::duration<double>(end - start).count();
    }
    shutdown();
    if (!quit) { std::cerr << "Runtime fixture deadline expired\n"; return 3; }
    if (inputActive()) return 4;
    if (mode == "manual_lease" && !sawScenarioInputAfterLease)
    {
        std::cerr << "Scenario input was lost when the manual lease expired\n";
        return 5;
    }
    std::cout << "Runtime handled network quit and released held input\n";
}
