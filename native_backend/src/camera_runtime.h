#pragma once

#include <condition_variable>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include <opencv2/core/mat.hpp>

class CameraRuntime {
public:
    struct CaptureSettings {
        std::string device_path;
        std::string pipeline_description;
        int width = 1920;
        int height = 1080;
        int framerate = 60;
        int jpeg_quality = 88;
        double timeout_seconds = 1.0;
        std::optional<int> rotate_code;
        std::optional<int> flip_code;
    };

    CameraRuntime();
    ~CameraRuntime();

    std::string start();
    void stop();
    bool is_running() const;
    std::string source_name() const;
    std::vector<unsigned char> get_frame_bytes(double timeout_seconds = 2.0);

private:
    void capture_loop();
    CaptureSettings load_settings() const;
    std::string wait_for_first_frame_locked(double timeout_seconds);

    mutable std::mutex mutex_;
    std::condition_variable condition_;
    std::thread worker_;
    bool stop_requested_ = false;
    bool running_ = false;
    std::vector<unsigned char> latest_jpeg_;
    std::string source_name_;
    std::string last_error_;
};
