#include "breath_analyzer.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <fcntl.h>
#include <fstream>
#include <numeric>
#include <optional>
#include <random>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#if defined(__linux__)
#include <linux/spi/spidev.h>
#include <sys/ioctl.h>
#include <unistd.h>
#endif

namespace {

constexpr unsigned char kCmdPidStartup = 0x10;
constexpr unsigned char kCmdPidShutdown = 0x11;
constexpr unsigned char kCmdAdcInit = 0x20;
constexpr unsigned char kCmdAdcStop = 0x21;
constexpr unsigned char kCmdReadAdc = 0x30;

int int_env(const char* key, int default_value) {
    const char* raw = std::getenv(key);
    if (raw == nullptr) {
        return default_value;
    }
    try {
        return std::stoi(std::string(raw));
    } catch (const std::exception&) {
        return default_value;
    }
}

double double_env(const char* key, double default_value) {
    const char* raw = std::getenv(key);
    if (raw == nullptr) {
        return default_value;
    }
    try {
        return std::stod(std::string(raw));
    } catch (const std::exception&) {
        return default_value;
    }
}

std::string string_env(const char* key, const std::string& default_value) {
    const char* raw = std::getenv(key);
    if (raw == nullptr) {
        return default_value;
    }
    const std::string value(raw);
    return value.empty() ? default_value : value;
}

std::string to_lower_copy(std::string value) {
    std::transform(
        value.begin(),
        value.end(),
        value.begin(),
        [](unsigned char ch) { return static_cast<char>(std::tolower(ch)); }
    );
    return value;
}

bool truthy_env(const char* key, bool default_value) {
    const char* raw = std::getenv(key);
    if (raw == nullptr) {
        return default_value;
    }
    const std::string normalized = to_lower_copy(std::string(raw));
    return normalized != "0" && normalized != "false" && normalized != "no" && normalized != "off";
}

BreathReading build_breath_reading(
    double alcohol_ppb,
    double cannabis_ppb,
    std::optional<double> raw_sensor_value
) {
    const double alcohol_threshold = double_env("ATTENDANCE_BREATH_ALCOHOL_THRESHOLD_PPB", 35.0);
    const double cannabis_threshold = double_env("ATTENDANCE_BREATH_CANNABIS_THRESHOLD_PPB", 25.0);
    const double alcohol_value = std::max(0.0, alcohol_ppb);
    const double cannabis_value = std::max(0.0, cannabis_ppb);
    std::optional<double> raw_value;
    if (raw_sensor_value.has_value()) {
        raw_value = std::max(0.0, *raw_sensor_value);
    }
    return BreathReading{
        alcohol_value,
        cannabis_value,
        alcohol_value <= alcohol_threshold,
        cannabis_value <= cannabis_threshold,
        raw_value,
    };
}

void sleep_for_seconds(double seconds) {
    if (seconds > 0.0) {
        std::this_thread::sleep_for(std::chrono::duration<double>(seconds));
    }
}

#if defined(__linux__)
class ScopedFileDescriptor {
public:
    explicit ScopedFileDescriptor(int fd = -1) : fd_(fd) {}
    ~ScopedFileDescriptor() {
        if (fd_ >= 0) {
            close(fd_);
        }
    }

    ScopedFileDescriptor(const ScopedFileDescriptor&) = delete;
    ScopedFileDescriptor& operator=(const ScopedFileDescriptor&) = delete;

    ScopedFileDescriptor(ScopedFileDescriptor&& other) noexcept : fd_(other.fd_) {
        other.fd_ = -1;
    }

    ScopedFileDescriptor& operator=(ScopedFileDescriptor&& other) noexcept {
        if (this != &other) {
            if (fd_ >= 0) {
                close(fd_);
            }
            fd_ = other.fd_;
            other.fd_ = -1;
        }
        return *this;
    }

    int get() const { return fd_; }

private:
    int fd_ = -1;
};

void write_text_file(const std::string& path, const std::string& value) {
    std::ofstream stream(path);
    if (!stream) {
        throw std::runtime_error("Could not write to " + path + ".");
    }
    stream << value;
}

class ScopedGpio {
public:
    explicit ScopedGpio(int gpio_number) : gpio_number_(gpio_number) {
        const std::string gpio_path = "/sys/class/gpio/gpio" + std::to_string(gpio_number_);
        if (!std::ifstream(gpio_path + "/direction")) {
            try {
                write_text_file("/sys/class/gpio/export", std::to_string(gpio_number_));
                sleep_for_seconds(0.02);
                exported_ = true;
            } catch (const std::exception&) {
                // Ignore export errors if the GPIO is already exported.
            }
        }
        write_text_file(gpio_path + "/direction", "out");
        write(false);
    }

    ~ScopedGpio() {
        try {
            write(false);
        } catch (const std::exception&) {
        }
        if (exported_) {
            try {
                write_text_file("/sys/class/gpio/unexport", std::to_string(gpio_number_));
            } catch (const std::exception&) {
            }
        }
    }

    void write(bool enabled) {
        const std::string gpio_path = "/sys/class/gpio/gpio" + std::to_string(gpio_number_) + "/value";
        write_text_file(gpio_path, enabled ? "1" : "0");
    }

private:
    int gpio_number_ = 0;
    bool exported_ = false;
};

ScopedFileDescriptor open_spi_device(const std::string& device, int mode, int speed_hz) {
    ScopedFileDescriptor fd(open(device.c_str(), O_RDWR));
    if (fd.get() < 0) {
        throw std::runtime_error("Could not open SPI device " + device + ".");
    }

    std::uint8_t spi_mode = static_cast<std::uint8_t>(mode);
    std::uint8_t bits = 8;
    std::uint32_t speed = static_cast<std::uint32_t>(speed_hz);
    if (ioctl(fd.get(), SPI_IOC_WR_MODE, &spi_mode) < 0 ||
        ioctl(fd.get(), SPI_IOC_WR_BITS_PER_WORD, &bits) < 0 ||
        ioctl(fd.get(), SPI_IOC_WR_MAX_SPEED_HZ, &speed) < 0) {
        throw std::runtime_error("Could not configure the SPI device.");
    }
    return fd;
}

std::array<unsigned char, 4> spi_transfer(int fd, unsigned char command) {
    std::array<unsigned char, 4> tx = {command, 0x00, 0x00, 0x00};
    std::array<unsigned char, 4> rx = {0x00, 0x00, 0x00, 0x00};

    spi_ioc_transfer transfer {};
    transfer.tx_buf = reinterpret_cast<unsigned long>(tx.data());
    transfer.rx_buf = reinterpret_cast<unsigned long>(rx.data());
    transfer.len = static_cast<std::uint32_t>(tx.size());
    transfer.bits_per_word = 8;

    if (ioctl(fd, SPI_IOC_MESSAGE(1), &transfer) < 0) {
        throw std::runtime_error("The SPI board transfer failed.");
    }

    return rx;
}
#endif

BreathReading perform_mock_read() {
    return build_breath_reading(
        double_env("ATTENDANCE_BREATH_MOCK_ALCOHOL_PPB", 0.0),
        double_env("ATTENDANCE_BREATH_MOCK_CANNABIS_PPB", 0.0),
        std::nullopt
    );
}

BreathReading perform_spi_read() {
#if !defined(__linux__)
    throw std::runtime_error("Live SPI breath analyzer is only supported on Linux.");
#else
    const std::string spi_device = string_env("ATTENDANCE_BREATH_SPI_DEVICE", "/dev/spidev1.0");
    const int spi_mode = int_env("ATTENDANCE_BREATH_SPI_MODE", 0);
    const int spi_speed_hz = int_env("ATTENDANCE_BREATH_SPI_SPEED_HZ", 1000000);
    const int board_enable_gpio = int_env("ATTENDANCE_BREATH_BOARD_ENABLE_GPIO", 257);
    const double power_settle_seconds = double_env("ATTENDANCE_BREATH_POWER_SETTLE_SECONDS", 0.01);
    const double pid_settle_seconds = double_env("ATTENDANCE_BREATH_PID_SETTLE_SECONDS", 0.01);
    const double adc_settle_seconds = double_env("ATTENDANCE_BREATH_ADC_SETTLE_SECONDS", 0.01);
    const double sample_seconds = std::max(0.0, double_env("ATTENDANCE_BREATH_SAMPLE_SECONDS", 10.0));
    const double sample_interval_seconds = std::max(0.0, double_env("ATTENDANCE_BREATH_SAMPLE_INTERVAL_SECONDS", 0.05));
    const std::string sample_aggregation = to_lower_copy(string_env("ATTENDANCE_BREATH_SAMPLE_AGGREGATION", "mean"));
    const int adc_bits = std::max(1, int_env("ATTENDANCE_BREATH_ADC_BITS", 16));
    const double adc_vref = double_env("ATTENDANCE_BREATH_ADC_VREF", 2.5);
    const double adc_gain = double_env("ATTENDANCE_BREATH_ADC_GAIN", 2.0);
    const double adc_baseline = double_env("ATTENDANCE_BREATH_ADC_BASELINE", 0.0);
    const std::string alcohol_source = to_lower_copy(string_env("ATTENDANCE_BREATH_ALCOHOL_SOURCE", "mock"));
    const double alcohol_scale = double_env("ATTENDANCE_BREATH_ADC_TO_ALCOHOL_SCALE", 1.0);
    const double alcohol_offset = double_env("ATTENDANCE_BREATH_ADC_TO_ALCOHOL_OFFSET", 0.0);
    const double placeholder_alcohol_min_ppb = double_env("ATTENDANCE_BREATH_PLACEHOLDER_ALCOHOL_MIN_PPB", 0.0);
    const double placeholder_alcohol_max_ppb = double_env("ATTENDANCE_BREATH_PLACEHOLDER_ALCOHOL_MAX_PPB", 10.0);
    const double cannabis_scale = double_env("ATTENDANCE_BREATH_ADC_TO_CANNABIS_SCALE", 1.0);
    const double cannabis_offset = double_env("ATTENDANCE_BREATH_ADC_TO_CANNABIS_OFFSET", 0.0);

    ScopedGpio gpio(board_enable_gpio);
    ScopedFileDescriptor spi = open_spi_device(spi_device, spi_mode, spi_speed_hz);

    std::vector<int> samples;
    std::optional<double> raw_sensor_value;
    try {
        gpio.write(true);
        sleep_for_seconds(power_settle_seconds);

        spi_transfer(spi.get(), kCmdPidStartup);
        sleep_for_seconds(pid_settle_seconds);

        spi_transfer(spi.get(), kCmdAdcInit);
        sleep_for_seconds(adc_settle_seconds);

        const auto deadline = std::chrono::steady_clock::now() + std::chrono::duration<double>(sample_seconds);
        while (true) {
            const auto response = spi_transfer(spi.get(), kCmdReadAdc);
            samples.push_back((static_cast<int>(response[1]) << 8) | static_cast<int>(response[2]));
            if (std::chrono::steady_clock::now() >= deadline) {
                break;
            }
            sleep_for_seconds(sample_interval_seconds);
        }
    } catch (const std::exception& exc) {
        throw std::runtime_error("Live breath analyzer read failed: " + std::string(exc.what()));
    }

    try {
        spi_transfer(spi.get(), kCmdAdcStop);
        sleep_for_seconds(adc_settle_seconds);
        spi_transfer(spi.get(), kCmdPidShutdown);
    } catch (const std::exception&) {
    }
    gpio.write(false);

    if (samples.empty()) {
        throw std::runtime_error("Live breath analyzer read failed: no ADC samples were returned.");
    }

    double aggregated_adc = 0.0;
    if (sample_aggregation == "peak") {
        aggregated_adc = static_cast<double>(*std::max_element(samples.begin(), samples.end()));
    } else if (sample_aggregation == "last") {
        aggregated_adc = static_cast<double>(samples.back());
    } else {
        aggregated_adc = std::accumulate(samples.begin(), samples.end(), 0.0) / static_cast<double>(samples.size());
    }

    double alcohol_ppb = 0.0;
    if (alcohol_source == "adc") {
        const double adjusted_adc = std::max(0.0, aggregated_adc - adc_baseline);
        alcohol_ppb = std::max(0.0, (adjusted_adc * alcohol_scale) + alcohol_offset);
        raw_sensor_value = aggregated_adc;
    } else {
        std::mt19937 generator(std::random_device{}());
        std::uniform_real_distribution<double> distribution(
            std::min(placeholder_alcohol_min_ppb, placeholder_alcohol_max_ppb),
            std::max(placeholder_alcohol_min_ppb, placeholder_alcohol_max_ppb)
        );
        alcohol_ppb = std::max(0.0, distribution(generator));
    }

    if (adc_gain <= 0.0) {
        throw std::runtime_error("Live breath analyzer read failed: ADC gain must be greater than zero.");
    }

    const double full_scale = std::pow(2.0, static_cast<double>(adc_bits));
    const double converted_value = std::max(0.0, aggregated_adc) * (adc_vref / (full_scale * adc_gain));
    const double cannabis_ppb = std::max(0.0, (converted_value * cannabis_scale) + cannabis_offset);

    return build_breath_reading(alcohol_ppb, cannabis_ppb, raw_sensor_value);
#endif
}

}  // namespace

NativeBreathAnalyzer::NativeBreathAnalyzer() {
    sample_seconds_ = std::max(0.0, double_env("ATTENDANCE_BREATH_SAMPLE_SECONDS", 10.0));
    const std::string configured_mode = to_lower_copy(string_env("ATTENDANCE_BREATH_ANALYZER_MODE", "mock"));
    if (configured_mode == "live" || configured_mode == "spi" || configured_mode == "hardware") {
        mode_ = Mode::Spi;
        name_ = "spi";
        if (to_lower_copy(string_env("ATTENDANCE_BREATH_ALCOHOL_SOURCE", "mock")) != "adc") {
            startup_warnings_.push_back(
                "Alcohol readings are currently placeholder values. The cannabis reading is live from the SPI breath board, but the alcohol script is not wired yet."
            );
        }
        return;
    }

    if (configured_mode != "mock") {
        startup_warnings_.push_back(
            "Unsupported breath analyzer mode '" + configured_mode + "'. Falling back to mock readings."
        );
    }
    mode_ = Mode::Mock;
    name_ = "mock";
}

const std::string& NativeBreathAnalyzer::name() const {
    return name_;
}

double NativeBreathAnalyzer::sample_seconds() const {
    return sample_seconds_;
}

const std::vector<std::string>& NativeBreathAnalyzer::startup_warnings() const {
    return startup_warnings_;
}

BreathReading NativeBreathAnalyzer::read(int worker_id, const std::string& camera_id) const {
    (void)worker_id;
    (void)camera_id;
    if (mode_ == Mode::Spi) {
        return perform_spi_read();
    }
    return perform_mock_read();
}
