#pragma once

#include <optional>
#include <string>
#include <vector>

struct BreathReading {
    double alcohol_ppb = 0.0;
    double cannabis_ppb = 0.0;
    bool alcohol_clear = true;
    bool cannabis_clear = true;
    std::optional<double> raw_sensor_value;
};

class NativeBreathAnalyzer {
public:
    NativeBreathAnalyzer();

    const std::string& name() const;
    double sample_seconds() const;
    const std::vector<std::string>& startup_warnings() const;
    BreathReading read(int worker_id, const std::string& camera_id) const;

private:
    enum class Mode {
        Mock,
        Spi,
    };

    Mode mode_ = Mode::Mock;
    std::string name_ = "mock";
    double sample_seconds_ = 10.0;
    std::vector<std::string> startup_warnings_;
};
