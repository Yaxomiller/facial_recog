#pragma once

#include <filesystem>
#include <optional>
#include <string>
#include <vector>

#include "detector.h"

struct CandidateDebug {
    int worker_id = 0;
    double score = 0.0;
};

struct FaceDebug {
    int face_index = 0;
    bool accepted = false;
    std::string reason;
    std::optional<double> blur_variance;
    std::optional<double> brightness;
    std::optional<int> eyes_detected;
    std::vector<CandidateDebug> candidates;
};

struct RecognitionMatch {
    int worker_id = 0;
    std::string employee_code;
    std::string name;
    double score = 0.0;
    bool attendance_marked = false;
    std::string source = "fresh";
};

struct RecognitionResult {
    std::vector<RecognitionMatch> matches;
    int unknown_faces = 0;
    int detected_faces = 0;
    std::vector<DetectionBox> boxes;
    std::vector<FaceDebug> debug_faces;
};

class NativeRecognizer {
public:
    NativeRecognizer();

    RecognitionResult recognize(
        const std::filesystem::path& database_path,
        const std::vector<unsigned char>& image_bytes,
        const std::string& camera_id,
        int top_k = 3
    );

    std::vector<float> prepare_enrollment_embedding(
        const std::vector<unsigned char>& image_bytes
    );

private:
    FaceDetector detector_;
};
