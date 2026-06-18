#include "detector.h"

#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <stdexcept>
#include <string>
#include <vector>

#include <opencv2/imgproc.hpp>
#include <opencv2/objdetect.hpp>

namespace fs = std::filesystem;

FaceDetector::FaceDetector() = default;

std::string FaceDetector::backend_name() const {
    return "opencv-cascade";
}

std::vector<std::string> FaceDetector::cascade_paths() const {
    std::vector<std::string> paths;
    if (const char* custom_dir = std::getenv("ATTENDANCE_OPENCV_HAAR_DIR")) {
        const fs::path base(custom_dir);
        paths.push_back((base / "haarcascade_frontalface_default.xml").string());
        paths.push_back((base / "haarcascade_frontalface_alt.xml").string());
        paths.push_back((base / "haarcascade_frontalface_alt2.xml").string());
    }

    for (const fs::path base : {
             fs::path("/usr/share/opencv4/haarcascades"),
             fs::path("/usr/share/opencv/haarcascades"),
         }) {
        paths.push_back((base / "haarcascade_frontalface_default.xml").string());
        paths.push_back((base / "haarcascade_frontalface_alt.xml").string());
        paths.push_back((base / "haarcascade_frontalface_alt2.xml").string());
    }
    return paths;
}

std::vector<DetectionBox> FaceDetector::detect(const cv::Mat& image) const {
    if (image.empty()) {
        return {};
    }

    cv::Mat gray;
    if (image.channels() == 1) {
        gray = image.clone();
    } else {
        cv::cvtColor(image, gray, cv::COLOR_BGR2GRAY);
    }
    cv::equalizeHist(gray, gray);
    cv::GaussianBlur(gray, gray, cv::Size(5, 5), 0);

    const int min_width = std::max(48, gray.cols / 10);
    const int min_height = std::max(48, gray.rows / 10);

    std::vector<DetectionBox> candidates;
    for (const std::string& cascade_path : cascade_paths()) {
        cv::CascadeClassifier cascade;
        if (!cascade.load(cascade_path)) {
            continue;
        }
        for (const auto& setting : {std::pair<double, int>{1.1, 4}, std::pair<double, int>{1.05, 3}, std::pair<double, int>{1.2, 5}}) {
            std::vector<cv::Rect> faces;
            cascade.detectMultiScale(
                gray,
                faces,
                setting.first,
                setting.second,
                0,
                cv::Size(min_width, min_height)
            );
            for (const cv::Rect& face : faces) {
                candidates.push_back(DetectionBox{face.x, face.y, face.width, face.height});
            }
        }
    }

    return deduplicate(candidates);
}

std::vector<DetectionBox> FaceDetector::deduplicate(const std::vector<DetectionBox>& faces) {
    std::vector<DetectionBox> sorted = faces;
    std::sort(
        sorted.begin(),
        sorted.end(),
        [](const DetectionBox& left, const DetectionBox& right) {
            return (left.width * left.height) > (right.width * right.height);
        }
    );

    std::vector<DetectionBox> deduped;
    for (const DetectionBox& face : sorted) {
        bool overlaps = false;
        for (const DetectionBox& existing : deduped) {
            if (iou(face, existing) > 0.35f) {
                overlaps = true;
                break;
            }
        }
        if (!overlaps) {
            deduped.push_back(face);
        }
    }
    return deduped;
}

float FaceDetector::iou(const DetectionBox& a, const DetectionBox& b) {
    const int left = std::max(a.x, b.x);
    const int top = std::max(a.y, b.y);
    const int right = std::min(a.x + a.width, b.x + b.width);
    const int bottom = std::min(a.y + a.height, b.y + b.height);
    if (right <= left || bottom <= top) {
        return 0.0f;
    }

    const float intersection = static_cast<float>((right - left) * (bottom - top));
    const float union_area = static_cast<float>(
        (a.width * a.height) + (b.width * b.height) - intersection
    );
    if (union_area <= 0.0f) {
        return 0.0f;
    }
    return intersection / union_area;
}
