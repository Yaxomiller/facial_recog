#include "recognizer.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstring>
#include <cstdlib>
#include <filesystem>
#include <map>
#include <mutex>
#include <numeric>
#include <optional>
#include <stdexcept>
#include <string>
#include <tuple>
#include <unordered_map>
#include <utility>
#include <vector>

#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>
#include <opencv2/objdetect.hpp>
#include <sqlite3.h>

namespace fs = std::filesystem;

namespace {

constexpr int kFaceWidth = 112;
constexpr int kFaceHeight = 112;
constexpr int kMaxTopK = 5;
constexpr int kMaxFacesPerRequestDefault = 10;
constexpr int kMinEnrollmentImagesDefault = 3;

struct SqliteConnection {
    explicit SqliteConnection(const fs::path& path) {
        if (sqlite3_open(path.string().c_str(), &handle) != SQLITE_OK) {
            std::string message = sqlite3_errmsg(handle);
            if (handle != nullptr) {
                sqlite3_close(handle);
                handle = nullptr;
            }
            throw std::runtime_error("Could not open SQLite database: " + message);
        }
        sqlite3_busy_timeout(handle, 30000);
    }

    ~SqliteConnection() {
        if (handle != nullptr) {
            sqlite3_close(handle);
        }
    }

    sqlite3* handle = nullptr;
};

struct PendingMatch {
    int worker_id = 0;
    int count = 0;
    double best_score = 0.0;
    double total_score = 0.0;
    double min_score = 0.0;
    std::chrono::steady_clock::time_point expires_at;
};

struct WorkerRecord {
    int worker_id = 0;
    std::string employee_code;
    std::string name;
};

struct TrainingSample {
    int worker_id = 0;
    std::vector<float> embedding;
};

struct WorkerProfile {
    std::vector<float> centroid;
    double min_similarity = 0.0;
    double mean_similarity = 0.0;
    int sample_count = 0;
};

struct SearchHit {
    int worker_id = 0;
    double score = 0.0;
};

struct DescriptorConsensus {
    std::optional<int> worker_id;
    double best_score = 0.0;
    double second_score = 0.0;
    std::vector<double> support_scores;
    std::vector<float> representative_embedding;
    int variant_hits = 0;
    std::vector<CandidateDebug> candidates;
    std::optional<std::string> rejection_reason;
};

std::mutex g_pending_mutex;
std::unordered_map<std::string, PendingMatch> g_pending_matches;

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

int sqlite_column_int_or(sqlite3_stmt* statement, int column_index, int default_value) {
    if (sqlite3_column_type(statement, column_index) == SQLITE_NULL) {
        return default_value;
    }
    return sqlite3_column_int(statement, column_index);
}

std::string sqlite_column_text(sqlite3_stmt* statement, int column_index) {
    const unsigned char* raw = sqlite3_column_text(statement, column_index);
    if (raw == nullptr) {
        return "";
    }
    return std::string(reinterpret_cast<const char*>(raw));
}

std::string trim(std::string value) {
    auto not_space = [](unsigned char ch) { return !std::isspace(ch); };
    value.erase(value.begin(), std::find_if(value.begin(), value.end(), not_space));
    value.erase(std::find_if(value.rbegin(), value.rend(), not_space).base(), value.end());
    return value;
}

std::vector<std::string> haar_base_dirs() {
    std::vector<std::string> bases;
    if (const char* custom_dir = std::getenv("ATTENDANCE_OPENCV_HAAR_DIR")) {
        const std::string value = trim(std::string(custom_dir));
        if (!value.empty()) {
            bases.push_back(value);
        }
    }
    bases.push_back("/usr/share/opencv4/haarcascades");
    bases.push_back("/usr/share/opencv/haarcascades");
    return bases;
}

std::vector<std::string> eye_cascade_paths() {
    std::vector<std::string> paths;
    for (const std::string& base : haar_base_dirs()) {
        paths.push_back((fs::path(base) / "haarcascade_eye_tree_eyeglasses.xml").string());
        paths.push_back((fs::path(base) / "haarcascade_eye.xml").string());
    }
    return paths;
}

std::optional<cv::CascadeClassifier>& eye_cascade() {
    static std::optional<cv::CascadeClassifier> cascade = []() -> std::optional<cv::CascadeClassifier> {
        for (const std::string& path : eye_cascade_paths()) {
            cv::CascadeClassifier classifier;
            if (classifier.load(path)) {
                return classifier;
            }
        }
        return std::nullopt;
    }();
    return cascade;
}

std::vector<cv::Rect> detect_eyes(const cv::Mat& face_roi, double scale_factor = 1.1, int min_neighbors = 4) {
    std::vector<cv::Rect> eyes;
    auto& cascade = eye_cascade();
    if (!cascade.has_value()) {
        return eyes;
    }
    cascade->detectMultiScale(face_roi, eyes, scale_factor, min_neighbors, 0, cv::Size(20, 20));
    return eyes;
}

cv::Mat prepare_face_gray(const cv::Mat& face_image) {
    cv::Mat gray;
    if (face_image.channels() == 1) {
        gray = face_image.clone();
    } else {
        cv::cvtColor(face_image, gray, cv::COLOR_BGR2GRAY);
    }
    cv::equalizeHist(gray, gray);
    return gray;
}

std::optional<std::pair<cv::Point2f, cv::Point2f>> select_eye_pair(
    const std::vector<cv::Rect>& eyes,
    int image_width,
    int image_height
) {
    if (eyes.size() < 2) {
        return std::nullopt;
    }

    std::vector<cv::Point2f> centers;
    for (const cv::Rect& eye : eyes) {
        const float center_x = static_cast<float>(eye.x) + (static_cast<float>(eye.width) / 2.0f);
        const float center_y = static_cast<float>(eye.y) + (static_cast<float>(eye.height) / 2.0f);
        if (center_y > static_cast<float>(image_height) * 0.68f) {
            continue;
        }
        centers.emplace_back(center_x, center_y);
    }

    if (centers.size() < 2) {
        return std::nullopt;
    }

    std::optional<std::pair<cv::Point2f, cv::Point2f>> best_pair;
    float best_score = -1e9f;
    const float target_y = static_cast<float>(image_height) * 0.38f;

    for (std::size_t index = 0; index + 1 < centers.size(); ++index) {
        for (std::size_t inner = index + 1; inner < centers.size(); ++inner) {
            cv::Point2f first = centers[index];
            cv::Point2f second = centers[inner];
            if (first.x == second.x) {
                continue;
            }
            cv::Point2f left_eye = first.x < second.x ? first : second;
            cv::Point2f right_eye = first.x < second.x ? second : first;
            const float horizontal_gap = right_eye.x - left_eye.x;
            const float vertical_gap = std::abs(right_eye.y - left_eye.y);
            if (horizontal_gap < static_cast<float>(image_width) * 0.18f ||
                horizontal_gap > static_cast<float>(image_width) * 0.75f) {
                continue;
            }
            if (vertical_gap > static_cast<float>(image_height) * 0.18f) {
                continue;
            }
            const float mean_y = (left_eye.y + right_eye.y) / 2.0f;
            const float symmetry_penalty = std::abs(mean_y - target_y) * 0.35f;
            const float score = horizontal_gap - (vertical_gap * 1.3f) - symmetry_penalty;
            if (score > best_score) {
                best_score = score;
                best_pair = std::make_pair(left_eye, right_eye);
            }
        }
    }

    return best_pair;
}

std::pair<cv::Mat, int> align_face_by_eyes(const cv::Mat& face_image, const cv::Size& output_size) {
    cv::Mat prepared;
    cv::resize(prepare_face_gray(face_image), prepared, output_size);
    const std::vector<cv::Rect> eyes = detect_eyes(prepared, 1.08, 3);
    const auto eye_pair = select_eye_pair(eyes, output_size.width, output_size.height);
    if (!eye_pair.has_value()) {
        return {prepared, static_cast<int>(eyes.size())};
    }

    const cv::Point2f left_eye = eye_pair->first;
    const cv::Point2f right_eye = eye_pair->second;
    std::vector<cv::Point2f> source = {left_eye, right_eye};
    std::vector<cv::Point2f> destination = {
        cv::Point2f(static_cast<float>(output_size.width) * 0.34f, static_cast<float>(output_size.height) * 0.38f),
        cv::Point2f(static_cast<float>(output_size.width) * 0.66f, static_cast<float>(output_size.height) * 0.38f),
    };

    cv::Mat transform = cv::estimateAffinePartial2D(source, destination, cv::noArray(), cv::LMEDS);
    if (transform.empty()) {
        return {prepared, static_cast<int>(eyes.size())};
    }

    cv::Mat aligned;
    cv::warpAffine(
        prepared,
        aligned,
        transform,
        output_size,
        cv::INTER_LINEAR,
        cv::BORDER_REPLICATE
    );
    cv::equalizeHist(aligned, aligned);
    return {aligned, static_cast<int>(eyes.size())};
}

std::vector<cv::Mat> split_grid(const cv::Mat& image, int rows, int cols) {
    std::vector<cv::Mat> cells;
    for (int row = 0; row < rows; ++row) {
        const int top = (image.rows * row) / rows;
        const int bottom = (image.rows * (row + 1)) / rows;
        for (int col = 0; col < cols; ++col) {
            const int left = (image.cols * col) / cols;
            const int right = (image.cols * (col + 1)) / cols;
            cells.push_back(image(cv::Range(top, bottom), cv::Range(left, right)));
        }
    }
    return cells;
}

std::vector<float> normalize_part(const std::vector<float>& input) {
    double norm = 0.0;
    for (float value : input) {
        norm += static_cast<double>(value) * static_cast<double>(value);
    }
    norm = std::sqrt(norm);
    if (norm <= 0.0) {
        return input;
    }

    std::vector<float> output = input;
    for (float& value : output) {
        value = static_cast<float>(value / norm);
    }
    return output;
}

std::vector<float> histogram_values(const cv::Mat& image, int bins) {
    const float range[] = {0.0f, 256.0f};
    const float* ranges[] = {range};
    cv::Mat histogram;
    const int channels[] = {0};
    const int hist_size[] = {bins};
    cv::calcHist(&image, 1, channels, cv::Mat(), histogram, 1, hist_size, ranges);
    std::vector<float> values(static_cast<std::size_t>(histogram.total()));
    for (int index = 0; index < histogram.rows; ++index) {
        values[static_cast<std::size_t>(index)] = histogram.at<float>(index);
    }
    return values;
}

std::vector<float> gradient_histogram(const cv::Mat& image, int rows, int cols, int bins) {
    cv::Mat grad_x;
    cv::Mat grad_y;
    cv::Sobel(image, grad_x, CV_32F, 1, 0, 3);
    cv::Sobel(image, grad_y, CV_32F, 0, 1, 3);

    cv::Mat magnitude;
    cv::Mat angle;
    cv::cartToPolar(grad_x, grad_y, magnitude, angle, true);

    std::vector<float> descriptor;
    descriptor.reserve(static_cast<std::size_t>(rows * cols * bins));
    for (int row = 0; row < rows; ++row) {
        const int top = (image.rows * row) / rows;
        const int bottom = (image.rows * (row + 1)) / rows;
        for (int col = 0; col < cols; ++col) {
            const int left = (image.cols * col) / cols;
            const int right = (image.cols * (col + 1)) / cols;
            std::vector<float> hist(static_cast<std::size_t>(bins), 0.0f);
            for (int y = top; y < bottom; ++y) {
                for (int x = left; x < right; ++x) {
                    float angle_value = std::fmod(angle.at<float>(y, x), 180.0f);
                    if (angle_value < 0.0f) {
                        angle_value += 180.0f;
                    }
                    int bin = static_cast<int>((angle_value / 180.0f) * bins);
                    if (bin >= bins) {
                        bin = bins - 1;
                    }
                    hist[static_cast<std::size_t>(bin)] += magnitude.at<float>(y, x);
                }
            }
            descriptor.insert(descriptor.end(), hist.begin(), hist.end());
        }
    }
    return descriptor;
}

std::vector<float> lbp_histogram(const cv::Mat& image, int bins) {
    std::vector<float> hist(static_cast<std::size_t>(bins), 0.0f);
    if (image.rows < 3 || image.cols < 3) {
        return hist;
    }

    for (int y = 1; y < image.rows - 1; ++y) {
        for (int x = 1; x < image.cols - 1; ++x) {
            const unsigned char center = image.at<unsigned char>(y, x);
            unsigned char lbp = 0;
            const std::array<cv::Point, 8> offsets = {
                cv::Point(-1, -1), cv::Point(0, -1), cv::Point(1, -1), cv::Point(1, 0),
                cv::Point(1, 1), cv::Point(0, 1), cv::Point(-1, 1), cv::Point(-1, 0),
            };
            for (int bit = 0; bit < static_cast<int>(offsets.size()); ++bit) {
                const cv::Point point = offsets[static_cast<std::size_t>(bit)];
                if (image.at<unsigned char>(y + point.y, x + point.x) >= center) {
                    lbp = static_cast<unsigned char>(lbp | (1U << bit));
                }
            }
            const int grouped = (static_cast<int>(lbp) * bins) / 256;
            hist[static_cast<std::size_t>(std::min(bins - 1, grouped))] += 1.0f;
        }
    }

    return hist;
}

std::vector<float> low_frequency_dct(const cv::Mat& image, int rows, int cols) {
    cv::Mat normalized;
    image.convertTo(normalized, CV_32F, 1.0 / 255.0);
    cv::Mat coefficients;
    cv::dct(normalized, coefficients);

    std::vector<float> values;
    values.reserve(static_cast<std::size_t>(rows * cols));
    for (int row = 0; row < std::min(rows, coefficients.rows); ++row) {
        for (int col = 0; col < std::min(cols, coefficients.cols); ++col) {
            values.push_back(coefficients.at<float>(row, col));
        }
    }
    if (values.size() <= 1) {
        return values;
    }
    return std::vector<float>(values.begin() + 1, values.end());
}

std::vector<float> prepare_descriptor_face(const cv::Mat& face_image) {
    const auto aligned_result = align_face_by_eyes(face_image, cv::Size(kFaceWidth, kFaceHeight));
    cv::Mat normalized = aligned_result.first;

    cv::Ptr<cv::CLAHE> clahe = cv::createCLAHE(2.0, cv::Size(8, 8));
    cv::Mat clahe_applied;
    clahe->apply(normalized, clahe_applied);

    cv::Mat denoised;
    cv::bilateralFilter(clahe_applied, denoised, 5, 18, 18);

    cv::Mat blurred;
    cv::GaussianBlur(denoised, blurred, cv::Size(0, 0), 1.1);

    cv::Mat sharpened;
    cv::addWeighted(denoised, 1.2, blurred, -0.2, 0.0, sharpened);

    cv::Mat mask = cv::Mat::zeros(sharpened.size(), sharpened.type());
    cv::ellipse(
        mask,
        cv::Point(sharpened.cols / 2, sharpened.rows / 2),
        cv::Size(static_cast<int>(sharpened.cols * 0.42), static_cast<int>(sharpened.rows * 0.48)),
        0,
        0,
        360,
        cv::Scalar(255),
        -1
    );

    cv::Mat prepared;
    cv::bitwise_and(sharpened, mask, prepared);

    std::vector<float> global_hist = normalize_part(histogram_values(prepared, 32));

    std::vector<float> regional_hist;
    for (const cv::Mat& region : split_grid(prepared, 2, 2)) {
        const std::vector<float> part = normalize_part(histogram_values(region, 16));
        regional_hist.insert(regional_hist.end(), part.begin(), part.end());
    }

    std::vector<float> gradient_hist = normalize_part(gradient_histogram(prepared, 4, 4, 8));
    std::vector<float> lbp_hist = normalize_part(lbp_histogram(prepared, 32));
    std::vector<float> dct_features = normalize_part(low_frequency_dct(prepared, 7, 7));

    std::vector<float> descriptor;
    descriptor.reserve(
        global_hist.size() + regional_hist.size() + gradient_hist.size() + lbp_hist.size() + dct_features.size()
    );
    descriptor.insert(descriptor.end(), global_hist.begin(), global_hist.end());
    descriptor.insert(descriptor.end(), regional_hist.begin(), regional_hist.end());
    descriptor.insert(descriptor.end(), gradient_hist.begin(), gradient_hist.end());
    descriptor.insert(descriptor.end(), lbp_hist.begin(), lbp_hist.end());
    descriptor.insert(descriptor.end(), dct_features.begin(), dct_features.end());

    for (float& value : descriptor) {
        value = std::sqrt(std::max(0.0f, value));
    }
    return normalize_part(descriptor);
}

double dot_product(const std::vector<float>& left, const std::vector<float>& right) {
    const std::size_t count = std::min(left.size(), right.size());
    double total = 0.0;
    for (std::size_t index = 0; index < count; ++index) {
        total += static_cast<double>(left[index]) * static_cast<double>(right[index]);
    }
    return total;
}

std::vector<float> normalized_copy(const std::vector<float>& input) {
    return normalize_part(input);
}

std::vector<float> vector_from_blob(const void* blob, int bytes) {
    if (blob == nullptr || bytes <= 0 || (bytes % static_cast<int>(sizeof(float))) != 0) {
        return {};
    }

    const int count = bytes / static_cast<int>(sizeof(float));
    std::vector<float> output(static_cast<std::size_t>(count));
    std::memcpy(output.data(), blob, static_cast<std::size_t>(bytes));
    return normalized_copy(output);
}

std::vector<TrainingSample> fetch_training_samples(const fs::path& database_path, std::size_t dimension) {
    SqliteConnection connection(database_path);
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.handle,
            "SELECT worker_id, vector FROM worker_embeddings ORDER BY id ASC",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.handle));
    }

    std::vector<TrainingSample> samples;
    while (sqlite3_step(statement) == SQLITE_ROW) {
        const int worker_id = sqlite3_column_int(statement, 0);
        const void* blob = sqlite3_column_blob(statement, 1);
        const int bytes = sqlite3_column_bytes(statement, 1);
        std::vector<float> vector = vector_from_blob(blob, bytes);
        if (vector.size() != dimension) {
            continue;
        }
        samples.push_back(TrainingSample{worker_id, std::move(vector)});
    }
    sqlite3_finalize(statement);
    return samples;
}

std::map<int, WorkerRecord> fetch_workers(const fs::path& database_path) {
    SqliteConnection connection(database_path);
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.handle,
            "SELECT id, employee_code, name FROM workers",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.handle));
    }

    std::map<int, WorkerRecord> workers;
    while (sqlite3_step(statement) == SQLITE_ROW) {
        const int worker_id = sqlite3_column_int(statement, 0);
        workers[worker_id] = WorkerRecord{
            worker_id,
            sqlite_column_text(statement, 1),
            sqlite_column_text(statement, 2),
        };
    }
    sqlite3_finalize(statement);
    return workers;
}

std::map<int, WorkerProfile> build_worker_profiles(const std::vector<TrainingSample>& samples) {
    std::map<int, std::vector<std::vector<float>>> grouped;
    for (const TrainingSample& sample : samples) {
        grouped[sample.worker_id].push_back(sample.embedding);
    }

    std::map<int, WorkerProfile> profiles;
    for (const auto& [worker_id, vectors] : grouped) {
        if (vectors.empty()) {
            continue;
        }
        std::vector<float> centroid(vectors.front().size(), 0.0f);
        for (const std::vector<float>& vector : vectors) {
            for (std::size_t index = 0; index < vector.size(); ++index) {
                centroid[index] += vector[index];
            }
        }
        for (float& value : centroid) {
            value /= static_cast<float>(vectors.size());
        }
        centroid = normalized_copy(centroid);

        std::vector<double> similarities;
        similarities.reserve(vectors.size());
        for (const std::vector<float>& vector : vectors) {
            similarities.push_back(dot_product(vector, centroid));
        }

        profiles[worker_id] = WorkerProfile{
            centroid,
            *std::min_element(similarities.begin(), similarities.end()),
            std::accumulate(similarities.begin(), similarities.end(), 0.0) / static_cast<double>(similarities.size()),
            static_cast<int>(vectors.size()),
        };
    }
    return profiles;
}

std::vector<SearchHit> search_embeddings(
    const std::vector<float>& query_embedding,
    const std::vector<TrainingSample>& samples,
    int top_k
) {
    std::vector<SearchHit> hits;
    hits.reserve(samples.size());
    for (const TrainingSample& sample : samples) {
        hits.push_back(SearchHit{sample.worker_id, dot_product(query_embedding, sample.embedding)});
    }

    std::sort(
        hits.begin(),
        hits.end(),
        [](const SearchHit& left, const SearchHit& right) {
            return left.score > right.score;
        }
    );

    if (static_cast<int>(hits.size()) > top_k) {
        hits.resize(static_cast<std::size_t>(top_k));
    }
    return hits;
}

std::optional<cv::Mat> tighten_face_crop(const cv::Mat& face_crop, double trim_ratio = 0.08) {
    const int trim_x = static_cast<int>(face_crop.cols * trim_ratio);
    const int trim_y = static_cast<int>(face_crop.rows * trim_ratio);
    if (trim_x <= 0 || trim_y <= 0) {
        return std::nullopt;
    }
    if ((face_crop.cols - (trim_x * 2)) < 48 || (face_crop.rows - (trim_y * 2)) < 48) {
        return std::nullopt;
    }
    return face_crop(cv::Range(trim_y, face_crop.rows - trim_y), cv::Range(trim_x, face_crop.cols - trim_x)).clone();
}

DescriptorConsensus descriptor_consensus(
    const cv::Mat& face_crop,
    const std::vector<TrainingSample>& samples,
    int top_k
) {
    std::vector<cv::Mat> variants = {face_crop};
    if (const auto tighter = tighten_face_crop(face_crop)) {
        variants.push_back(*tighter);
    }

    std::vector<std::vector<float>> embeddings;
    embeddings.reserve(variants.size());
    for (const cv::Mat& variant : variants) {
        embeddings.push_back(prepare_descriptor_face(variant));
    }

    std::vector<std::vector<SearchHit>> search_results;
    search_results.reserve(embeddings.size());
    for (const std::vector<float>& embedding : embeddings) {
        search_results.push_back(search_embeddings(embedding, samples, top_k));
    }

    bool any_hits = false;
    for (const auto& hits : search_results) {
        if (!hits.empty()) {
            any_hits = true;
            break;
        }
    }
    if (!any_hits) {
        return DescriptorConsensus{
            std::nullopt,
            0.0,
            0.0,
            {},
            {},
            0,
            {},
            std::string("Rejected: no similar enrolled face found."),
        };
    }

    std::map<int, std::vector<double>> worker_variant_best_scores;
    std::map<int, std::vector<double>> worker_support_scores;
    std::map<int, double> worker_max_scores;
    std::vector<int> variant_winner_worker_ids;
    std::vector<std::vector<float>> variant_winner_embeddings;
    std::vector<double> variant_winner_scores;

    for (std::size_t index = 0; index < embeddings.size(); ++index) {
        const std::vector<SearchHit>& hits = search_results[index];
        if (hits.empty()) {
            continue;
        }

        std::map<int, double> grouped_scores;
        for (const SearchHit& hit : hits) {
            grouped_scores[hit.worker_id] = std::max(grouped_scores[hit.worker_id], hit.score);
            worker_support_scores[hit.worker_id].push_back(hit.score);
            worker_max_scores[hit.worker_id] = std::max(worker_max_scores[hit.worker_id], hit.score);
        }
        if (grouped_scores.empty()) {
            continue;
        }

        std::vector<std::pair<int, double>> ranked_scores(grouped_scores.begin(), grouped_scores.end());
        std::sort(
            ranked_scores.begin(),
            ranked_scores.end(),
            [](const auto& left, const auto& right) {
                return left.second > right.second;
            }
        );

        const int winner_worker_id = ranked_scores.front().first;
        const double winner_score = ranked_scores.front().second;
        worker_variant_best_scores[winner_worker_id].push_back(winner_score);
        variant_winner_worker_ids.push_back(winner_worker_id);
        variant_winner_embeddings.push_back(embeddings[index]);
        variant_winner_scores.push_back(winner_score);
    }

    if (worker_variant_best_scores.empty()) {
        return DescriptorConsensus{
            std::nullopt,
            0.0,
            0.0,
            {},
            {},
            0,
            {},
            std::string("Rejected: descriptor verification returned no stable candidate."),
        };
    }

    struct RankedCandidate {
        int worker_id = 0;
        int votes = 0;
        double average_score = 0.0;
        double peak_score = 0.0;
    };

    std::vector<RankedCandidate> ranked_candidates;
    for (const auto& [worker_id, scores] : worker_variant_best_scores) {
        const double average = std::accumulate(scores.begin(), scores.end(), 0.0) / static_cast<double>(scores.size());
        ranked_candidates.push_back(RankedCandidate{
            worker_id,
            static_cast<int>(scores.size()),
            average,
            worker_max_scores[worker_id],
        });
    }

    std::sort(
        ranked_candidates.begin(),
        ranked_candidates.end(),
        [](const RankedCandidate& left, const RankedCandidate& right) {
            return std::tie(left.votes, left.average_score, left.peak_score) >
                   std::tie(right.votes, right.average_score, right.peak_score);
        }
    );

    const RankedCandidate& best = ranked_candidates.front();
    const double second_score = ranked_candidates.size() > 1 ? ranked_candidates[1].average_score : 0.0;

    std::vector<float> representative_embedding;
    double best_embedding_score = -1e9;
    for (std::size_t index = 0; index < variant_winner_embeddings.size(); ++index) {
        if (variant_winner_worker_ids[index] != best.worker_id) {
            continue;
        }
        if (variant_winner_scores[index] > best_embedding_score) {
            representative_embedding = variant_winner_embeddings[index];
            best_embedding_score = variant_winner_scores[index];
        }
    }

    std::vector<CandidateDebug> candidates;
    for (std::size_t index = 0; index < std::min<std::size_t>(3, ranked_candidates.size()); ++index) {
        candidates.push_back(CandidateDebug{
            ranked_candidates[index].worker_id,
            std::max(ranked_candidates[index].average_score, ranked_candidates[index].peak_score),
        });
    }

    const int required_hits = std::min(
        int_env("ATTENDANCE_DESCRIPTOR_VARIANT_REQUIRED_HITS", 2),
        static_cast<int>(variants.size())
    );

    if (best.votes < required_hits) {
        return DescriptorConsensus{
            std::nullopt,
            std::max(best.average_score, best.peak_score),
            second_score,
            worker_support_scores[best.worker_id],
            representative_embedding,
            best.votes,
            candidates,
            std::string("Rejected: face match was not stable across repeated descriptor checks."),
        };
    }

    if (ranked_candidates.size() > 1 && ranked_candidates[1].votes == best.votes) {
        return DescriptorConsensus{
            std::nullopt,
            std::max(best.average_score, best.peak_score),
            second_score,
            worker_support_scores[best.worker_id],
            representative_embedding,
            best.votes,
            candidates,
            std::string("Rejected: repeated descriptor checks disagreed on the employee identity."),
        };
    }

    return DescriptorConsensus{
        best.worker_id,
        std::max(best.average_score, best.peak_score),
        second_score,
        worker_support_scores[best.worker_id],
        representative_embedding,
        best.votes,
        candidates,
        std::nullopt,
    };
}

std::pair<double, double> face_quality_metrics(const cv::Mat& face_crop) {
    cv::Mat gray;
    if (face_crop.channels() == 1) {
        gray = face_crop;
    } else {
        cv::cvtColor(face_crop, gray, cv::COLOR_BGR2GRAY);
    }
    cv::Mat laplacian;
    cv::Laplacian(gray, laplacian, CV_64F);
    cv::Scalar mean;
    cv::Scalar stddev;
    cv::meanStdDev(laplacian, mean, stddev);
    const double blur_variance = stddev[0] * stddev[0];
    const double brightness = cv::mean(gray)[0];
    return {blur_variance, brightness};
}

bool is_face_usable(
    int face_width,
    int face_height,
    double blur_variance,
    double brightness
) {
    const int min_face_width = int_env("ATTENDANCE_MIN_FACE_WIDTH", 90);
    const int min_face_height = int_env("ATTENDANCE_MIN_FACE_HEIGHT", 90);
    const double min_blur_variance = double_env("ATTENDANCE_MIN_FACE_BLUR_VARIANCE", 30.0);
    (void)brightness; // Brightness is intentionally not gated: dark or bright frames are accepted as-is.

    return face_width >= min_face_width &&
           face_height >= min_face_height &&
           blur_variance >= min_blur_variance;
}

std::string face_rejection_reason(
    int face_width,
    int face_height,
    double blur_variance,
    double brightness
) {
    const int min_face_width = int_env("ATTENDANCE_MIN_FACE_WIDTH", 90);
    const int min_face_height = int_env("ATTENDANCE_MIN_FACE_HEIGHT", 90);
    const double min_blur_variance = double_env("ATTENDANCE_MIN_FACE_BLUR_VARIANCE", 30.0);
    (void)brightness; // Brightness is intentionally not gated: dark or bright frames are accepted as-is.

    std::vector<std::string> reasons;
    if (face_width < min_face_width || face_height < min_face_height) {
        reasons.push_back(
            "face is too small (" + std::to_string(face_width) + "x" + std::to_string(face_height) +
            ", minimum " + std::to_string(min_face_width) + "x" + std::to_string(min_face_height) + ")"
        );
    }
    if (blur_variance < min_blur_variance) {
        reasons.push_back(
            "face is too blurry (" + cv::format("%.1f", blur_variance) + " < " + cv::format("%.1f", min_blur_variance) + ")"
        );
    }
    if (reasons.empty()) {
        return "Rejected by the face quality gate.";
    }

    std::string output = "Rejected: ";
    for (std::size_t index = 0; index < reasons.size(); ++index) {
        if (index > 0) {
            output += "; ";
        }
        output += reasons[index];
    }
    output += ".";
    return output;
}

DetectionBox expand_face_box(
    const DetectionBox& box,
    int image_width,
    int image_height,
    double padding_ratio = 0.18
) {
    const int pad_x = static_cast<int>(box.width * padding_ratio);
    const int pad_y = static_cast<int>(box.height * padding_ratio);
    const int left = std::max(0, box.x - pad_x);
    const int top = std::max(0, box.y - pad_y);
    const int right = std::min(image_width, box.x + box.width + pad_x);
    const int bottom = std::min(image_height, box.y + box.height + pad_y);
    return DetectionBox{left, top, right - left, bottom - top};
}

double score_progress(double value, double floor, double ceiling) {
    if (ceiling <= floor) {
        return value >= ceiling ? 1.0 : 0.0;
    }
    return std::max(0.0, std::min(1.0, (value - floor) / (ceiling - floor)));
}

double adaptive_profile_score_threshold(
    const std::optional<WorkerProfile>& profile,
    int profile_count
) {
    const double match_threshold = double_env("ATTENDANCE_MATCH_THRESHOLD", 0.68);
    const double open_set_min_score = double_env("ATTENDANCE_OPEN_SET_MIN_SCORE", 0.72);
    const double baseline = std::max(match_threshold, open_set_min_score);
    if (!profile.has_value()) {
        return baseline;
    }
    if (profile_count <= 1) {
        const double single_min_score = double_env("ATTENDANCE_SINGLE_PROFILE_MIN_SCORE", 0.84);
        const double adaptive_floor = std::max(baseline, std::min(single_min_score, profile->mean_similarity - 0.16));
        return std::min(0.78, adaptive_floor);
    }
    return std::max(baseline, std::min(0.86, profile->mean_similarity - 0.18));
}

double adaptive_support_threshold(
    const std::optional<WorkerProfile>& profile,
    int profile_count
) {
    const double baseline = double_env("ATTENDANCE_OPEN_SET_SUPPORT_SCORE", 0.66);
    if (!profile.has_value()) {
        return baseline;
    }
    if (profile_count <= 1) {
        const double single_support = double_env("ATTENDANCE_SINGLE_PROFILE_SUPPORT_SCORE", 0.72);
        return std::max(baseline, std::max(single_support, std::min(0.82, profile->mean_similarity - 0.18)));
    }
    return std::max(baseline, std::min(0.82, profile->mean_similarity - 0.22));
}

std::pair<bool, std::string> passes_open_set_gate(
    int worker_id,
    double best_score,
    const std::vector<double>& support_scores,
    const std::vector<float>& query_embedding,
    const std::map<int, WorkerProfile>& worker_profiles
) {
    const auto profile_iter = worker_profiles.find(worker_id);
    const std::optional<WorkerProfile> profile =
        profile_iter == worker_profiles.end() ? std::nullopt : std::optional<WorkerProfile>(profile_iter->second);

    const int min_enrollment_images = int_env("ATTENDANCE_MIN_ENROLLMENT_IMAGES", kMinEnrollmentImagesDefault);
    if (profile.has_value() && profile->sample_count < min_enrollment_images) {
        return {
            false,
            "Rejected: employee has only " + std::to_string(profile->sample_count) +
                " enrolled face sample(s). Re-enroll with at least " + std::to_string(min_enrollment_images) + " clear photos.",
        };
    }

    const int profile_count = static_cast<int>(worker_profiles.size());
    const double minimum_score = adaptive_profile_score_threshold(profile, profile_count);
    const double support_threshold = adaptive_support_threshold(profile, profile_count);

    int required_support_hits = 2;
    if (profile_count <= 1 && profile.has_value()) {
        const int single_required = int_env("ATTENDANCE_SINGLE_PROFILE_REQUIRED_SUPPORT_HITS", 3);
        required_support_hits = std::max(2, std::min(profile->sample_count, single_required));
    }

    if (best_score < minimum_score) {
        return {
            false,
            "Rejected: best similarity score " + cv::format("%.3f", best_score) +
                " is below the verification threshold " + cv::format("%.3f", minimum_score) + ".",
        };
    }

    if (!profile.has_value()) {
        return {
            false,
            "Rejected: no enrolled descriptor profile is available for this employee. Re-enroll the employee.",
        };
    }

    const std::vector<float> normalized_query = normalized_copy(query_embedding);
    if (normalized_query.empty()) {
        return {false, "Rejected: descriptor could not be normalized."};
    }

    const double centroid_score = dot_product(normalized_query, profile->centroid);
    const double configured_centroid_floor = std::min(0.70, double_env("ATTENDANCE_OPEN_SET_MIN_CENTROID_SCORE", 0.70));
    double centroid_threshold = configured_centroid_floor;
    if (profile_count <= 1) {
        centroid_threshold = std::min(
            std::min(0.70, double_env("ATTENDANCE_MAX_PROFILE_CENTROID_THRESHOLD", 0.70)),
            std::min(0.70, double_env("ATTENDANCE_SINGLE_PROFILE_MIN_CENTROID_SCORE", 0.70))
        );
    } else {
        centroid_threshold = std::min(
            std::min(0.70, double_env("ATTENDANCE_MAX_PROFILE_CENTROID_THRESHOLD", 0.70)),
            std::max(
                configured_centroid_floor,
                std::min(0.86, profile->mean_similarity - double_env("ATTENDANCE_OPEN_SET_CENTROID_MARGIN", 0.08))
            )
        );
    }
    if (centroid_score < centroid_threshold) {
        return {
            false,
            "Rejected: face is not close enough to the enrolled profile (" +
                cv::format("%.3f", centroid_score) + " < " + cv::format("%.3f", centroid_threshold) + ").",
        };
    }

    int strong_support_hits = 0;
    for (double score : support_scores) {
        if (score >= support_threshold) {
            strong_support_hits += 1;
        }
    }
    if (strong_support_hits < required_support_hits) {
        return {
            false,
            "Rejected: fewer than " + std::to_string(required_support_hits) +
                " enrolled samples matched strongly enough for a safe verification.",
        };
    }

    return {true, ""};
}

double calibrated_match_confidence(
    int worker_id,
    double raw_score,
    const std::vector<double>& support_scores,
    const std::vector<float>& query_embedding,
    const std::map<int, WorkerProfile>& worker_profiles
) {
    const auto profile_iter = worker_profiles.find(worker_id);
    const std::optional<WorkerProfile> profile =
        profile_iter == worker_profiles.end() ? std::nullopt : std::optional<WorkerProfile>(profile_iter->second);
    const int profile_count = static_cast<int>(worker_profiles.size());

    const double score_floor = adaptive_profile_score_threshold(profile, profile_count);
    const double support_floor = adaptive_support_threshold(profile, profile_count);
    double centroid_floor = std::min(0.70, double_env("ATTENDANCE_OPEN_SET_MIN_CENTROID_SCORE", 0.70));

    double profile_mean = profile.has_value() ? profile->mean_similarity : 0.86;
    double score_target = 0.0;
    double centroid_target = 0.0;
    double support_target = 0.0;
    if (profile_count <= 1) {
        centroid_floor = std::min(0.70, double_env("ATTENDANCE_SINGLE_PROFILE_MIN_CENTROID_SCORE", 0.70));
        profile_mean = profile.has_value() ? profile->mean_similarity : (score_floor + 0.08);
        score_target = std::min(0.79, std::max(score_floor + 0.05, profile_mean - 0.12));
        centroid_target = std::min(0.84, std::max(centroid_floor + 0.06, profile_mean - 0.08));
        support_target = std::min(0.80, std::max(support_floor + 0.05, score_target));
    } else {
        score_target = std::min(0.86, std::max(score_floor + 0.08, profile_mean - 0.08));
        centroid_target = std::min(0.87, std::max(centroid_floor + 0.06, profile_mean - 0.06));
        support_target = std::min(0.84, std::max(support_floor + 0.06, score_target - 0.02));
    }

    const double raw_component = score_progress(raw_score, score_floor, score_target);
    double support_anchor = raw_score;
    if (!support_scores.empty()) {
        std::vector<double> sorted_support = support_scores;
        std::sort(sorted_support.begin(), sorted_support.end(), std::greater<double>());
        const std::size_t used = std::min<std::size_t>(3, sorted_support.size());
        support_anchor = std::accumulate(sorted_support.begin(), sorted_support.begin() + used, 0.0) / static_cast<double>(used);
    }
    const double support_component = score_progress(support_anchor, support_floor, support_target);

    double centroid_component = raw_component;
    if (profile.has_value()) {
        const std::vector<float> normalized_query = normalized_copy(query_embedding);
        if (!normalized_query.empty()) {
            const double centroid_score = dot_product(normalized_query, profile->centroid);
            centroid_component = score_progress(centroid_score, centroid_floor, centroid_target);
        }
    }

    const double blended =
        (raw_component * 0.45) +
        (support_component * 0.20) +
        (centroid_component * 0.35);
    return std::max(0.0, std::min(0.99, 0.64 + (0.34 * blended)));
}

void purge_pending_matches_locked(const std::chrono::steady_clock::time_point& now) {
    for (auto iter = g_pending_matches.begin(); iter != g_pending_matches.end();) {
        if (iter->second.expires_at <= now) {
            iter = g_pending_matches.erase(iter);
        } else {
            ++iter;
        }
    }
}

std::tuple<bool, double, std::optional<std::string>> confirm_match_candidate(
    const std::string& camera_id,
    int worker_id,
    double score,
    bool strict_mode
) {
    const double fast_accept_score = double_env("ATTENDANCE_FAST_ACCEPT_SCORE", 0.94);
    if (!strict_mode && score >= fast_accept_score) {
        std::lock_guard<std::mutex> lock(g_pending_mutex);
        g_pending_matches.erase(camera_id);
        return {true, score, std::nullopt};
    }

    const auto now = std::chrono::steady_clock::now();
    const auto window_seconds = std::chrono::seconds(int_env("ATTENDANCE_MATCH_CONFIRMATION_WINDOW_SECONDS", 2));
    int required_frames = int_env("ATTENDANCE_MATCH_CONFIRMATION_FRAMES", 4);
    double min_average_score = double_env("ATTENDANCE_MATCH_CONFIRMATION_MIN_AVG_SCORE", 0.76);
    double min_best_score = double_env("ATTENDANCE_MATCH_CONFIRMATION_MIN_BEST_SCORE", 0.78);
    double min_floor_score = min_average_score;

    if (strict_mode) {
        required_frames = std::max(required_frames, int_env("ATTENDANCE_SINGLE_PROFILE_CONFIRMATION_FRAMES", 5));
        min_average_score = std::max(min_average_score, double_env("ATTENDANCE_SINGLE_PROFILE_CONFIRMATION_MIN_AVG_SCORE", 0.86));
        min_best_score = std::max(min_best_score, double_env("ATTENDANCE_SINGLE_PROFILE_CONFIRMATION_MIN_BEST_SCORE", 0.88));
        min_floor_score = std::max(min_floor_score, double_env("ATTENDANCE_SINGLE_PROFILE_CONFIRMATION_MIN_FLOOR_SCORE", 0.82));
    }

    std::lock_guard<std::mutex> lock(g_pending_mutex);
    purge_pending_matches_locked(now);
    PendingMatch& state = g_pending_matches[camera_id];
    if (state.count == 0 || state.worker_id != worker_id || state.expires_at <= now) {
        state = PendingMatch{
            worker_id,
            1,
            score,
            score,
            score,
            now + window_seconds,
        };
        return {false, score, std::nullopt};
    }

    state.count += 1;
    state.best_score = std::max(state.best_score, score);
    state.total_score += score;
    state.min_score = std::min(state.min_score, score);
    state.expires_at = now + window_seconds;

    if (state.count < required_frames) {
        return {false, state.best_score, std::nullopt};
    }

    const double average_score = state.total_score / static_cast<double>(std::max(1, state.count));
    const double confirmation_score = std::max(state.best_score, average_score);
    g_pending_matches.erase(camera_id);
    if (average_score < min_average_score || state.best_score < min_best_score || state.min_score < min_floor_score) {
        return {
            false,
            confirmation_score,
            "Rejected: repeated face checks were not strong enough for a safe confirmation "
            "(avg " + std::string(cv::format("%.3f", average_score)) +
            ", best " + std::string(cv::format("%.3f", state.best_score)) + ").",
        };
    }
    return {true, confirmation_score, std::nullopt};
}

RecognitionResult multiple_face_result(const std::vector<DetectionBox>& faces) {
    RecognitionResult result;
    result.unknown_faces = static_cast<int>(faces.size());
    result.detected_faces = static_cast<int>(faces.size());
    result.boxes = faces;
    for (std::size_t index = 0; index < faces.size(); ++index) {
        result.debug_faces.push_back(FaceDebug{
            static_cast<int>(index),
            false,
            "Multiple faces detected. Only one employee can scan at a time.",
            std::nullopt,
            std::nullopt,
            std::nullopt,
            {},
        });
    }
    return result;
}

}  // namespace

NativeRecognizer::NativeRecognizer() = default;

std::vector<float> NativeRecognizer::prepare_enrollment_embedding(
    const std::vector<unsigned char>& image_bytes
) {
    const cv::Mat image = cv::imdecode(image_bytes, cv::IMREAD_COLOR);
    if (image.empty()) {
        throw std::runtime_error("Could not decode the enrollment image.");
    }

    const std::vector<DetectionBox> faces = detector_.detect(image);
    if (faces.empty()) {
        throw std::runtime_error("No face detected in enrollment image.");
    }
    if (faces.size() != 1) {
        throw std::runtime_error("Each enrollment photo must contain exactly one face.");
    }

    const DetectionBox& face = faces.front();
    const DetectionBox expanded = expand_face_box(face, image.cols, image.rows, 0.18);
    const cv::Rect rect(expanded.x, expanded.y, expanded.width, expanded.height);
    const cv::Mat face_crop = image(rect).clone();

    const auto [blur_variance, brightness] = face_quality_metrics(face_crop);
    if (!is_face_usable(face.width, face.height, blur_variance, brightness)) {
        std::string reason = face_rejection_reason(face.width, face.height, blur_variance, brightness);
        const std::string prefix = "Rejected: ";
        if (reason.rfind(prefix, 0) == 0) {
            reason = reason.substr(prefix.size());
        }
        if (!reason.empty() && reason.back() == '.') {
            reason.pop_back();
        }
        std::transform(
            reason.begin(),
            reason.end(),
            reason.begin(),
            [](unsigned char ch) { return static_cast<char>(std::tolower(ch)); }
        );
        throw std::runtime_error("Enrollment photo rejected because " + reason + ".");
    }

    return prepare_descriptor_face(face_crop);
}

RecognitionResult NativeRecognizer::recognize(
    const fs::path& database_path,
    const std::vector<unsigned char>& image_bytes,
    const std::string& camera_id,
    int top_k
) {
    const cv::Mat image = cv::imdecode(image_bytes, cv::IMREAD_COLOR);
    if (image.empty()) {
        throw std::runtime_error("Could not decode image bytes.");
    }

    std::vector<DetectionBox> faces = detector_.detect(image);
    if (faces.empty()) {
        return RecognitionResult{};
    }
    if (faces.size() > 1) {
        return multiple_face_result(faces);
    }

    std::vector<DetectionBox> largest_first = faces;
    std::sort(
        largest_first.begin(),
        largest_first.end(),
        [](const DetectionBox& left, const DetectionBox& right) {
            return (left.width * left.height) > (right.width * right.height);
        }
    );
    const int max_faces = int_env("ATTENDANCE_MAX_FACES_PER_REQUEST", kMaxFacesPerRequestDefault);
    if (static_cast<int>(largest_first.size()) > max_faces) {
        largest_first.resize(static_cast<std::size_t>(max_faces));
    }

    RecognitionResult result;
    result.detected_faces = static_cast<int>(largest_first.size());
    result.boxes = largest_first;

    const std::vector<TrainingSample> samples = fetch_training_samples(database_path, 304);
    const std::map<int, WorkerProfile> worker_profiles = build_worker_profiles(samples);
    const std::map<int, WorkerRecord> workers = fetch_workers(database_path);

    if (samples.empty()) {
        result.unknown_faces = result.detected_faces;
        for (std::size_t index = 0; index < largest_first.size(); ++index) {
            result.debug_faces.push_back(FaceDebug{
                static_cast<int>(index),
                false,
                "No enrolled face samples are available for recognition. Re-enroll the employee with at least 3 clear photos.",
                std::nullopt,
                std::nullopt,
                std::nullopt,
                {},
            });
        }
        return result;
    }

    std::map<int, double> grouped_hits;
    const int normalized_top_k = std::max(1, std::min(kMaxTopK, top_k));

    for (std::size_t face_index = 0; face_index < largest_first.size(); ++face_index) {
        const DetectionBox expanded = expand_face_box(largest_first[face_index], image.cols, image.rows, 0.18);
        const cv::Rect rect(expanded.x, expanded.y, expanded.width, expanded.height);
        cv::Mat face_crop = image(rect).clone();

        const auto [blur_variance, brightness] = face_quality_metrics(face_crop);
        if (!is_face_usable(largest_first[face_index].width, largest_first[face_index].height, blur_variance, brightness)) {
            result.unknown_faces += 1;
            result.debug_faces.push_back(FaceDebug{
                static_cast<int>(face_index),
                false,
                face_rejection_reason(largest_first[face_index].width, largest_first[face_index].height, blur_variance, brightness),
                blur_variance,
                brightness,
                std::nullopt,
                {},
            });
            continue;
        }

        DescriptorConsensus consensus = descriptor_consensus(face_crop, samples, std::max(normalized_top_k, 5));
        const auto aligned_face = align_face_by_eyes(face_crop, cv::Size(kFaceWidth, kFaceHeight));
        FaceDebug debug_entry;
        debug_entry.face_index = static_cast<int>(face_index);
        debug_entry.accepted = false;
        debug_entry.reason = "Pending descriptor search.";
        debug_entry.blur_variance = blur_variance;
        debug_entry.brightness = brightness;
        debug_entry.eyes_detected = aligned_face.second;
        debug_entry.candidates = consensus.candidates;

        if (!consensus.worker_id.has_value() || consensus.representative_embedding.empty()) {
            result.unknown_faces += 1;
            debug_entry.reason = consensus.rejection_reason.value_or("Rejected: descriptor verification failed.");
            result.debug_faces.push_back(std::move(debug_entry));
            continue;
        }

        const double match_threshold = double_env("ATTENDANCE_MATCH_THRESHOLD", 0.68);
        if (consensus.best_score < std::max(match_threshold, 0.55)) {
            result.unknown_faces += 1;
            debug_entry.reason = "Rejected: top similarity score is below threshold.";
            result.debug_faces.push_back(std::move(debug_entry));
            continue;
        }

        const double ambiguity_margin = 0.015;
        if ((consensus.best_score - consensus.second_score) < std::max(ambiguity_margin, 0.03)) {
            result.unknown_faces += 1;
            debug_entry.reason = "Rejected: top candidates are too close to each other.";
            result.debug_faces.push_back(std::move(debug_entry));
            continue;
        }

        const auto gate = passes_open_set_gate(
            *consensus.worker_id,
            consensus.best_score,
            consensus.support_scores,
            consensus.representative_embedding,
            worker_profiles
        );
        if (!gate.first) {
            result.unknown_faces += 1;
            debug_entry.reason = gate.second;
            result.debug_faces.push_back(std::move(debug_entry));
            continue;
        }

        const double confidence_score = calibrated_match_confidence(
            *consensus.worker_id,
            consensus.best_score,
            consensus.support_scores,
            consensus.representative_embedding,
            worker_profiles
        );
        const bool strict_mode = worker_profiles.size() <= 1;
        const auto confirmation = confirm_match_candidate(
            camera_id,
            *consensus.worker_id,
            confidence_score,
            strict_mode
        );
        if (!std::get<0>(confirmation)) {
            result.unknown_faces += 1;
            debug_entry.reason = std::get<2>(confirmation).value_or("Pending: waiting for the same identity across more frames.");
            result.debug_faces.push_back(std::move(debug_entry));
            continue;
        }

        const double accepted_score = std::get<1>(confirmation);
        grouped_hits[*consensus.worker_id] = std::max(grouped_hits[*consensus.worker_id], accepted_score);
        debug_entry.accepted = true;
        debug_entry.reason = "Accepted: descriptor match passed threshold and stability checks.";
        result.debug_faces.push_back(std::move(debug_entry));
    }

    std::vector<std::pair<int, double>> ranked_matches(grouped_hits.begin(), grouped_hits.end());
    std::sort(
        ranked_matches.begin(),
        ranked_matches.end(),
        [](const auto& left, const auto& right) {
            return left.second > right.second;
        }
    );

    for (const auto& [worker_id, score] : ranked_matches) {
        const auto worker_iter = workers.find(worker_id);
        if (worker_iter == workers.end()) {
            continue;
        }
        result.matches.push_back(RecognitionMatch{
            worker_id,
            worker_iter->second.employee_code,
            worker_iter->second.name,
            score,
            false,
            "fresh",
        });
    }

    return result;
}
