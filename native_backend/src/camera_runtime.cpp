#include "camera_runtime.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>
#include <opencv2/videoio.hpp>

namespace {

int int_env(const char* name, int default_value) {
    const char* raw_value = std::getenv(name);
    if (raw_value == nullptr) {
        return default_value;
    }
    try {
        return std::stoi(std::string(raw_value));
    } catch (const std::exception&) {
        return default_value;
    }
}

double double_env(const char* name, double default_value) {
    const char* raw_value = std::getenv(name);
    if (raw_value == nullptr) {
        return default_value;
    }
    try {
        return std::stod(std::string(raw_value));
    } catch (const std::exception&) {
        return default_value;
    }
}

std::string string_env(const char* name, const std::string& default_value) {
    const char* raw_value = std::getenv(name);
    if (raw_value == nullptr) {
        return default_value;
    }
    const std::string value(raw_value);
    if (value.empty()) {
        return default_value;
    }
    return value;
}

bool rotate_enabled() {
    const std::string value = string_env("ATTENDANCE_CAMERA_ROTATE_180", "true");
    const std::string lowered = [&value]() {
        std::string result = value;
        std::transform(
            result.begin(),
            result.end(),
            result.begin(),
            [](unsigned char ch) { return static_cast<char>(std::tolower(ch)); }
        );
        return result;
    }();
    return lowered != "0" && lowered != "false" && lowered != "no" && lowered != "off";
}

std::optional<int> flip_code() {
    const std::string value = string_env("ATTENDANCE_CAMERA_FLIP_CODE", "1");
    std::string lowered = value;
    std::transform(
        lowered.begin(),
        lowered.end(),
        lowered.begin(),
        [](unsigned char ch) { return static_cast<char>(std::tolower(ch)); }
    );
    if (lowered.empty() || lowered == "none" || lowered == "off" || lowered == "disable" || lowered == "disabled") {
        return std::nullopt;
    }
    try {
        return std::stoi(lowered);
    } catch (const std::exception&) {
        return 1;
    }
}

std::string default_pipeline_description(const CameraRuntime::CaptureSettings& settings) {
    return
        "v4l2src device=" + settings.device_path + " "
        "en-awisp=1 en-largemode=0 ! "
        "video/x-raw,format=I420,width=" + std::to_string(settings.width) +
        ",height=" + std::to_string(settings.height) +
        ",framerate=" + std::to_string(settings.framerate) + "/1 ! "
        "videoconvert ! appsink drop=true max-buffers=1 sync=false";
}

std::optional<int> device_index_from_path(const std::string& device_path) {
    const std::string prefix = "/dev/video";
    if (device_path.rfind(prefix, 0) != 0) {
        return std::nullopt;
    }
    try {
        return std::stoi(device_path.substr(prefix.size()));
    } catch (const std::exception&) {
        return std::nullopt;
    }
}

}  // namespace

CameraRuntime::CameraRuntime() = default;

CameraRuntime::~CameraRuntime() {
    stop();
}

CameraRuntime::CaptureSettings CameraRuntime::load_settings() const {
    CaptureSettings settings;
    settings.device_path = string_env("ATTENDANCE_CAMERA_DEVICE", "/dev/video0");
    settings.width = int_env("ATTENDANCE_CAMERA_WIDTH", 1920);
    settings.height = int_env("ATTENDANCE_CAMERA_HEIGHT", 1080);
    settings.framerate = int_env("ATTENDANCE_CAMERA_FRAMERATE", 60);
    settings.timeout_seconds = std::max(0.1, double_env("ATTENDANCE_CAMERA_TIMEOUT_SECONDS", 1.0));
    settings.jpeg_quality = std::max(40, std::min(100, int_env("ATTENDANCE_LOCAL_CAMERA_JPEG_QUALITY", 88)));
    settings.rotate_code = rotate_enabled() ? std::optional<int>(cv::ROTATE_180) : std::nullopt;
    settings.flip_code = flip_code();

    const std::string configured_pipeline = string_env("ATTENDANCE_CAMERA_PIPELINE", "");
    settings.pipeline_description = configured_pipeline.empty()
        ? default_pipeline_description(settings)
        : configured_pipeline;
    return settings;
}

std::string CameraRuntime::start() {
    std::thread stale_worker;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (worker_.joinable() && !running_) {
            stale_worker = std::move(worker_);
        }
    }

    if (stale_worker.joinable()) {
        stale_worker.join();
    }

    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!worker_.joinable()) {
            stop_requested_ = false;
            running_ = false;
            latest_jpeg_.clear();
            source_name_.clear();
            last_error_.clear();
            worker_ = std::thread(&CameraRuntime::capture_loop, this);
        }
    }
    return wait_for_first_frame_locked(std::max(0.5, double_env("ATTENDANCE_LOCAL_CAMERA_STARTUP_TIMEOUT_SECONDS", 4.0)));
}

void CameraRuntime::stop() {
    std::thread worker;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        stop_requested_ = true;
        running_ = false;
        condition_.notify_all();
        worker = std::move(worker_);
    }

    if (worker.joinable()) {
        worker.join();
    }

    std::lock_guard<std::mutex> lock(mutex_);
    latest_jpeg_.clear();
    source_name_.clear();
    last_error_.clear();
}

bool CameraRuntime::is_running() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return worker_.joinable() && running_;
}

std::string CameraRuntime::source_name() const {
    std::lock_guard<std::mutex> lock(mutex_);
    return source_name_;
}

std::vector<unsigned char> CameraRuntime::get_frame_bytes(double timeout_seconds) {
    std::unique_lock<std::mutex> lock(mutex_);
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::duration<double>(timeout_seconds);
    while (latest_jpeg_.empty()) {
        if (!last_error_.empty()) {
            throw std::runtime_error(last_error_);
        }
        if (!worker_.joinable() || !running_) {
            throw std::runtime_error("Local camera is not running.");
        }
        if (condition_.wait_until(lock, deadline) == std::cv_status::timeout) {
            throw std::runtime_error("Timed out waiting for the local camera frame.");
        }
    }
    return latest_jpeg_;
}

std::string CameraRuntime::wait_for_first_frame_locked(double timeout_seconds) {
    std::unique_lock<std::mutex> lock(mutex_);
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::duration<double>(timeout_seconds);
    while (latest_jpeg_.empty()) {
        if (!last_error_.empty()) {
            throw std::runtime_error(last_error_);
        }
        if (!worker_.joinable()) {
            throw std::runtime_error("Could not start the local camera runtime.");
        }
        if (condition_.wait_until(lock, deadline) == std::cv_status::timeout) {
            throw std::runtime_error("Timed out waiting for the local camera runtime to deliver the first frame.");
        }
    }
    return source_name_.empty() ? "native-local-camera" : source_name_;
}

void CameraRuntime::capture_loop() {
    const CaptureSettings settings = load_settings();
    cv::VideoCapture capture;
    std::string resolved_source_name;

    if (capture.open(settings.pipeline_description, cv::CAP_GSTREAMER)) {
        resolved_source_name = string_env("ATTENDANCE_CAMERA_PIPELINE", "").empty()
            ? "Native GStreamer pipeline (" + settings.device_path + ")"
            : "Configured native GStreamer pipeline";
    } else {
        const auto device_index = device_index_from_path(settings.device_path);
        if (device_index.has_value()) {
            capture.open(*device_index, cv::CAP_V4L2);
        } else {
            capture.open(settings.device_path, cv::CAP_V4L2);
        }
        if (capture.isOpened()) {
            capture.set(cv::CAP_PROP_FRAME_WIDTH, settings.width);
            capture.set(cv::CAP_PROP_FRAME_HEIGHT, settings.height);
            capture.set(cv::CAP_PROP_FPS, settings.framerate);
            resolved_source_name = "Native V4L2 camera (" + settings.device_path + ")";
        }
    }

    if (!capture.isOpened()) {
        std::lock_guard<std::mutex> lock(mutex_);
        last_error_ = "Could not start the native local camera. Check the GStreamer pipeline or /dev/video device.";
        running_ = false;
        condition_.notify_all();
        return;
    }

    {
        std::lock_guard<std::mutex> lock(mutex_);
        running_ = true;
        source_name_ = resolved_source_name;
        condition_.notify_all();
    }

    try {
        while (true) {
            {
                std::lock_guard<std::mutex> lock(mutex_);
                if (stop_requested_) {
                    break;
                }
            }

            cv::Mat frame;
            if (!capture.read(frame) || frame.empty()) {
                throw std::runtime_error("Could not read frame from the native local camera.");
            }

            if (settings.rotate_code.has_value()) {
                cv::rotate(frame, frame, *settings.rotate_code);
            }
            if (settings.flip_code.has_value()) {
                cv::flip(frame, frame, *settings.flip_code);
            }

            std::vector<unsigned char> encoded;
            const std::vector<int> encoding_params = {cv::IMWRITE_JPEG_QUALITY, settings.jpeg_quality};
            if (!cv::imencode(".jpg", frame, encoded, encoding_params)) {
                throw std::runtime_error("Could not encode local camera frame.");
            }

            {
                std::lock_guard<std::mutex> lock(mutex_);
                latest_jpeg_ = std::move(encoded);
                condition_.notify_all();
            }

            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
    } catch (const std::exception& exc) {
        std::lock_guard<std::mutex> lock(mutex_);
        last_error_ = exc.what();
        running_ = false;
        condition_.notify_all();
    }

    capture.release();

    {
        std::lock_guard<std::mutex> lock(mutex_);
        running_ = false;
        condition_.notify_all();
    }
}
