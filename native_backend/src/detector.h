#pragma once

#include <string>
#include <vector>

#include <opencv2/core/mat.hpp>

struct DetectionBox {
    int x = 0;
    int y = 0;
    int width = 0;
    int height = 0;
};

class FaceDetector {
public:
    FaceDetector();

    std::string backend_name() const;
    std::vector<DetectionBox> detect(const cv::Mat& image) const;

private:
    std::vector<std::string> cascade_paths() const;
    static float iou(const DetectionBox& a, const DetectionBox& b);
    static std::vector<DetectionBox> deduplicate(const std::vector<DetectionBox>& faces);
};
