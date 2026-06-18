#include <algorithm>
#include <array>
#include <chrono>
#include <cctype>
#include <cerrno>
#include <cstddef>
#include <cstdlib>
#include <ctime>
#include <cstring>
#include <filesystem>
#include <future>
#include <iomanip>
#include <iostream>
#include <map>
#include <mutex>
#include <numeric>
#include <optional>
#include <random>
#include <regex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

#include <opencv2/imgcodecs.hpp>
#include <sqlite3.h>

#include "breath_analyzer.h"
#include "camera_runtime.h"
#include "detector.h"
#include "recognizer.h"

#if defined(__linux__) || defined(__APPLE__)
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

#if defined(__linux__)
#include <crypt.h>
#endif

namespace fs = std::filesystem;

namespace {

constexpr int kListenBacklog = 64;
constexpr int kRequestChunkSize = 4096;
constexpr int kMaxRequestBytes = 8 * 1024 * 1024;
constexpr int kSessionTtlSecondsDefault = 43200;
constexpr int kDefaultAttendanceLimit = 100;
constexpr int kMaxAttendanceLimit = 500;
constexpr int kMinPasswordLength = 10;
constexpr int kRecoveryCodeLength = 6;
constexpr int kRecoveryTtlSecondsDefault = 600;
constexpr int kMinEnrollmentImagesDefault = 3;
constexpr int kAttendanceCooldownHoursDefault = 12;
constexpr const char* kNativeEmbedderName = "native-cpp-descriptor";
constexpr const char* kNativeIndexName = "sqlite-on-demand";

struct Config {
    fs::path data_dir;
    fs::path scalable_db_path;
    fs::path session_db_path;
    std::string host;
    int port;
    int session_ttl_seconds;
    std::string requested_embedder;
    std::string requested_index;
    bool fallback_enabled;
};

struct HttpRequest {
    std::string method;
    std::string target;
    std::string path;
    std::string query;
    std::map<std::string, std::string> headers;
    std::string body;
};

struct HttpResponse {
    int status = 200;
    std::string content_type = "application/json";
    std::string body;
    std::map<std::string, std::string> headers;
};

struct AuthStatus {
    bool configured = false;
    bool setup_required = true;
    std::string source = "none";
    bool email_configured = false;
    bool email_recovery_enabled = false;
};

struct LocalCredentials {
    std::string username;
    std::string password_hash;
    std::optional<std::string> email;
};

struct SessionState {
    std::string session_id;
    std::string username;
    std::string expires_at;
};

struct MultipartPart {
    std::string name;
    std::optional<std::string> filename;
    std::string content_type;
    std::string body;
};

struct WorkerRow {
    int id = 0;
    std::string employee_code;
    std::string name;
    std::string created_at;
};

struct AttendanceEventRecord {
    int id = 0;
    int worker_id = 0;
    std::string camera_id;
    double matched_score = 0.0;
    std::optional<double> raw_sensor_value;
    double alcohol_ppb = 0.0;
    double cannabis_ppb = 0.0;
    bool alcohol_clear = true;
    bool cannabis_clear = true;
    bool attendance_marked = false;
    std::string created_at;
};

struct RecoveryCodeRecord {
    int id = 0;
    std::string code;
};

struct PendingBreathSession {
    std::string session_id;
    int worker_id = 0;
    std::string camera_id;
    std::string started_at;
    double sample_seconds = 0.0;
    std::future<BreathReading> future;
    bool canceled = false;
    bool completed = false;
};

class SqliteConnection {
public:
    explicit SqliteConnection(const fs::path& path) {
        if (sqlite3_open(path.string().c_str(), &handle_) != SQLITE_OK) {
            std::string message = sqlite3_errmsg(handle_);
            if (handle_ != nullptr) {
                sqlite3_close(handle_);
                handle_ = nullptr;
            }
            throw std::runtime_error("Could not open SQLite database: " + message);
        }
        sqlite3_busy_timeout(handle_, 30000);
    }

    ~SqliteConnection() {
        if (handle_ != nullptr) {
            sqlite3_close(handle_);
        }
    }

    sqlite3* get() const {
        return handle_;
    }

private:
    sqlite3* handle_ = nullptr;
};

std::string trim(std::string value) {
    auto not_space = [](unsigned char ch) { return !std::isspace(ch); };
    value.erase(value.begin(), std::find_if(value.begin(), value.end(), not_space));
    value.erase(std::find_if(value.rbegin(), value.rend(), not_space).base(), value.end());
    return value;
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

std::optional<std::string> getenv_optional(const std::string& key) {
    const char* raw_value = std::getenv(key.c_str());
    if (raw_value == nullptr) {
        return std::nullopt;
    }
    return std::string(raw_value);
}

std::optional<std::string> first_env(const std::vector<std::string>& keys) {
    for (const std::string& key : keys) {
        if (auto value = getenv_optional(key)) {
            const std::string trimmed = trim(*value);
            if (!trimmed.empty()) {
                return trimmed;
            }
        }
    }
    return std::nullopt;
}

bool truthy_env(const std::string& key, bool default_value) {
    if (auto value = getenv_optional(key)) {
        const std::string normalized = to_lower_copy(trim(*value));
        return normalized != "0" && normalized != "false" && normalized != "no" && normalized != "off";
    }
    return default_value;
}

int int_env(const std::string& key, int default_value) {
    if (auto value = getenv_optional(key)) {
        try {
            return std::stoi(trim(*value));
        } catch (const std::exception&) {
            return default_value;
        }
    }
    return default_value;
}

std::string iso_utc_now() {
    const auto now = std::chrono::system_clock::now();
    const std::time_t now_time = std::chrono::system_clock::to_time_t(now);
    std::tm utc_tm{};
#if defined(_WIN32)
    gmtime_s(&utc_tm, &now_time);
#else
    gmtime_r(&now_time, &utc_tm);
#endif
    std::ostringstream stream;
    stream << std::put_time(&utc_tm, "%Y-%m-%dT%H:%M:%S+00:00");
    return stream.str();
}

std::string iso_utc_after_seconds(int ttl_seconds) {
    const auto target = std::chrono::system_clock::now() + std::chrono::seconds(ttl_seconds);
    const std::time_t target_time = std::chrono::system_clock::to_time_t(target);
    std::tm utc_tm{};
#if defined(_WIN32)
    gmtime_s(&utc_tm, &target_time);
#else
    gmtime_r(&target_time, &utc_tm);
#endif
    std::ostringstream stream;
    stream << std::put_time(&utc_tm, "%Y-%m-%dT%H:%M:%S+00:00");
    return stream.str();
}

std::string random_session_id() {
    static thread_local std::mt19937_64 generator(std::random_device{}());
    static const std::array<char, 16> hex_chars = {'0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'a', 'b', 'c', 'd', 'e', 'f'};
    std::uniform_int_distribution<int> distribution(0, 15);
    std::string output;
    output.reserve(36);
    for (int index = 0; index < 32; ++index) {
        if (index == 8 || index == 12 || index == 16 || index == 20) {
            output.push_back('-');
        }
        output.push_back(hex_chars[distribution(generator)]);
    }
    return output;
}

std::string url_decode(const std::string& value) {
    std::string output;
    output.reserve(value.size());
    for (std::size_t index = 0; index < value.size(); ++index) {
        if (value[index] == '%' && index + 2 < value.size()) {
            const std::string hex = value.substr(index + 1, 2);
            char decoded = static_cast<char>(std::strtol(hex.c_str(), nullptr, 16));
            output.push_back(decoded);
            index += 2;
        } else if (value[index] == '+') {
            output.push_back(' ');
        } else {
            output.push_back(value[index]);
        }
    }
    return output;
}

std::map<std::string, std::string> parse_query_string(const std::string& query) {
    std::map<std::string, std::string> values;
    std::size_t cursor = 0;
    while (cursor < query.size()) {
        const std::size_t separator = query.find('&', cursor);
        const std::string part = query.substr(cursor, separator == std::string::npos ? std::string::npos : separator - cursor);
        if (!part.empty()) {
            const std::size_t equals = part.find('=');
            const std::string key = url_decode(part.substr(0, equals));
            const std::string value = equals == std::string::npos ? "" : url_decode(part.substr(equals + 1));
            values[key] = value;
        }
        if (separator == std::string::npos) {
            break;
        }
        cursor = separator + 1;
    }
    return values;
}

std::string json_escape(const std::string& value) {
    std::ostringstream stream;
    for (unsigned char ch : value) {
        switch (ch) {
            case '\\':
                stream << "\\\\";
                break;
            case '"':
                stream << "\\\"";
                break;
            case '\b':
                stream << "\\b";
                break;
            case '\f':
                stream << "\\f";
                break;
            case '\n':
                stream << "\\n";
                break;
            case '\r':
                stream << "\\r";
                break;
            case '\t':
                stream << "\\t";
                break;
            default:
                if (ch < 0x20) {
                    stream << "\\u" << std::hex << std::setw(4) << std::setfill('0') << static_cast<int>(ch) << std::dec;
                } else {
                    stream << static_cast<char>(ch);
                }
                break;
        }
    }
    return stream.str();
}

std::string json_string(const std::string& value) {
    return "\"" + json_escape(value) + "\"";
}

std::string bool_json(bool value) {
    return value ? "true" : "false";
}

std::string sqlite_column_text(sqlite3_stmt* statement, int column_index) {
    const unsigned char* raw = sqlite3_column_text(statement, column_index);
    if (raw == nullptr) {
        return "";
    }
    return std::string(reinterpret_cast<const char*>(raw));
}

std::string sqlite_column_blob_text(sqlite3_stmt* statement, int column_index) {
    const auto* raw = static_cast<const unsigned char*>(sqlite3_column_blob(statement, column_index));
    const int length = sqlite3_column_bytes(statement, column_index);
    if (raw == nullptr || length <= 0) {
        return "";
    }
    return std::string(reinterpret_cast<const char*>(raw), static_cast<std::size_t>(length));
}

void exec_sql(sqlite3* database, const std::string& sql) {
    char* error_message = nullptr;
    if (sqlite3_exec(database, sql.c_str(), nullptr, nullptr, &error_message) != SQLITE_OK) {
        std::string message = error_message == nullptr ? "unknown SQLite error" : error_message;
        sqlite3_free(error_message);
        throw std::runtime_error(message);
    }
}

CameraRuntime& camera_runtime() {
    static CameraRuntime runtime;
    return runtime;
}

FaceDetector& face_detector() {
    static FaceDetector detector;
    return detector;
}

NativeRecognizer& native_recognizer() {
    static NativeRecognizer recognizer;
    return recognizer;
}

NativeBreathAnalyzer& breath_analyzer() {
    static NativeBreathAnalyzer analyzer;
    return analyzer;
}

std::mutex& breath_session_mutex() {
    static std::mutex mutex;
    return mutex;
}

std::unordered_map<std::string, PendingBreathSession>& breath_sessions() {
    static std::unordered_map<std::string, PendingBreathSession> sessions;
    return sessions;
}

Config load_config() {
    Config config{};

    fs::path base_dir = fs::current_path();
    if (auto configured_data_dir = getenv_optional("ATTENDANCE_DATA_DIR")) {
        const std::string trimmed = trim(*configured_data_dir);
        if (!trimmed.empty()) {
            base_dir = fs::path(trimmed);
        } else {
            base_dir = base_dir / "data";
        }
    } else {
        base_dir = base_dir / "data";
    }

    config.data_dir = base_dir;
    config.scalable_db_path = fs::path(
        getenv_optional("ATTENDANCE_DB_FILE").value_or((config.data_dir / "scalable_attendance.db").string())
    );
    config.session_db_path = fs::path(
        getenv_optional("ATTENDANCE_SESSION_DB_FILE").value_or((config.data_dir / "session_store.db").string())
    );
    config.host = trim(getenv_optional("ATTENDANCE_WEB_HOST").value_or("127.0.0.1"));
    if (config.host.empty()) {
        config.host = "127.0.0.1";
    }
    config.port = int_env("ATTENDANCE_WEB_PORT", 8000);
    config.session_ttl_seconds = int_env("ATTENDANCE_SESSION_TTL_SECONDS", kSessionTtlSecondsDefault);
    config.requested_embedder = trim(getenv_optional("ATTENDANCE_EMBEDDING_BACKEND").value_or("lbph"));
    if (config.requested_embedder.empty()) {
        config.requested_embedder = "lbph";
    }
    config.requested_index = trim(getenv_optional("ATTENDANCE_VECTOR_INDEX_BACKEND").value_or("numpy"));
    if (config.requested_index.empty()) {
        config.requested_index = "numpy";
    }
    config.fallback_enabled = truthy_env("ATTENDANCE_ALLOW_BACKEND_FALLBACK", true);
    return config;
}

void initialize_session_store(const Config& config) {
    fs::create_directories(config.session_db_path.parent_path());
    SqliteConnection connection(config.session_db_path);
    exec_sql(connection.get(), "PRAGMA journal_mode=WAL");
    exec_sql(connection.get(), "PRAGMA synchronous=NORMAL");
    exec_sql(
        connection.get(),
        R"sql(
            CREATE TABLE IF NOT EXISTS auth_sessions (
                session_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        )sql"
    );
    exec_sql(
        connection.get(),
        R"sql(
            CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires_at
            ON auth_sessions(expires_at)
        )sql"
    );
    exec_sql(
        connection.get(),
        R"sql(
            CREATE TABLE IF NOT EXISTS admin_credentials (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                username TEXT NOT NULL,
                password_hash BLOB NOT NULL,
                email TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        )sql"
    );
    exec_sql(
        connection.get(),
        R"sql(
            CREATE TABLE IF NOT EXISTS admin_recovery_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                purpose TEXT NOT NULL,
                email TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT
            )
        )sql"
    );
    exec_sql(
        connection.get(),
        R"sql(
            CREATE INDEX IF NOT EXISTS idx_admin_recovery_codes_lookup
            ON admin_recovery_codes(purpose, email, expires_at)
        )sql"
    );
}

void initialize_scalable_store(const Config& config) {
    fs::create_directories(config.scalable_db_path.parent_path());
    SqliteConnection connection(config.scalable_db_path);
    exec_sql(
        connection.get(),
        R"sql(
            CREATE TABLE IF NOT EXISTS workers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        )sql"
    );
    exec_sql(
        connection.get(),
        R"sql(
            CREATE TABLE IF NOT EXISTS worker_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id INTEGER NOT NULL,
                backend TEXT NOT NULL DEFAULT 'histogram',
                dimension INTEGER NOT NULL DEFAULT 0,
                face_image BLOB,
                vector BLOB NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (worker_id) REFERENCES workers(id)
            )
        )sql"
    );
    exec_sql(
        connection.get(),
        R"sql(
            CREATE TABLE IF NOT EXISTS attendance_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id INTEGER NOT NULL,
                camera_id TEXT NOT NULL,
                matched_score REAL NOT NULL,
                created_at TEXT NOT NULL,
                raw_sensor_value REAL,
                alcohol_ppb REAL NOT NULL DEFAULT 0,
                cannabis_ppb REAL NOT NULL DEFAULT 0,
                alcohol_clear INTEGER NOT NULL DEFAULT 1,
                cannabis_clear INTEGER NOT NULL DEFAULT 1,
                attendance_marked INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (worker_id) REFERENCES workers(id)
            )
        )sql"
    );
}

void purge_expired_sessions(sqlite3* database) {
    sqlite3_stmt* statement = nullptr;
    const std::string now = iso_utc_now();
    if (sqlite3_prepare_v2(
            database,
            "DELETE FROM auth_sessions WHERE expires_at <= ?",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(database));
    }
    sqlite3_bind_text(statement, 1, now.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(statement) != SQLITE_DONE) {
        std::string message = sqlite3_errmsg(database);
        sqlite3_finalize(statement);
        throw std::runtime_error(message);
    }
    sqlite3_finalize(statement);
}

std::optional<LocalCredentials> fetch_local_credentials(const Config& config) {
    SqliteConnection connection(config.session_db_path);
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            "SELECT username, password_hash, email FROM admin_credentials WHERE id = 1",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }

    std::optional<LocalCredentials> credentials;
    if (sqlite3_step(statement) == SQLITE_ROW) {
        LocalCredentials row{};
        row.username = sqlite_column_text(statement, 0);
        row.password_hash = sqlite_column_blob_text(statement, 1);
        const std::string email_value = sqlite_column_text(statement, 2);
        if (!email_value.empty()) {
            row.email = email_value;
        }
        credentials = row;
    }

    sqlite3_finalize(statement);
    return credentials;
}

AuthStatus get_auth_status(const Config& config) {
    if (const auto credentials = fetch_local_credentials(config)) {
        return AuthStatus{
            true,
            false,
            "local",
            credentials->email.has_value(),
            credentials->email.has_value(),
        };
    }

    const auto username = first_env({"ADMIN_USERNAME", "ATTENDANCE_ADMIN_USERNAME"});
    const auto password_hash = first_env({"ADMIN_PASSWORD_HASH", "ATTENDANCE_ADMIN_PASSWORD_HASH"});
    const auto email = first_env({"ADMIN_EMAIL", "ATTENDANCE_ADMIN_EMAIL"});
    if (username && password_hash) {
        return AuthStatus{
            true,
            false,
            "environment",
            email.has_value(),
            email.has_value(),
        };
    }

    return AuthStatus{};
}

std::optional<std::pair<std::string, std::string>> configured_admin_credentials(const Config& config) {
    if (const auto credentials = fetch_local_credentials(config)) {
        return std::make_pair(credentials->username, credentials->password_hash);
    }

    const auto username = first_env({"ADMIN_USERNAME", "ATTENDANCE_ADMIN_USERNAME"});
    const auto password_hash = first_env({"ADMIN_PASSWORD_HASH", "ATTENDANCE_ADMIN_PASSWORD_HASH"});
    if (username && password_hash) {
        return std::make_pair(*username, *password_hash);
    }
    return std::nullopt;
}

std::string normalize_username(std::string username);
std::string normalize_email(std::string email);
void validate_password(const std::string& password);
std::string hash_password(const std::string& password);

std::optional<std::string> configured_admin_email(const Config& config) {
    if (const auto credentials = fetch_local_credentials(config)) {
        if (credentials->email.has_value() && !trim(*credentials->email).empty()) {
            return normalize_email(*credentials->email);
        }
        return std::nullopt;
    }
    const auto env_email = first_env({"ADMIN_EMAIL", "ATTENDANCE_ADMIN_EMAIL"});
    if (!env_email.has_value()) {
        return std::nullopt;
    }
    return normalize_email(*env_email);
}

std::string normalize_username(std::string username) {
    username = trim(std::move(username));
    if (username.empty()) {
        throw std::runtime_error("Username cannot be empty.");
    }
    static const std::regex pattern(R"(^[A-Za-z0-9._@-]{3,64}$)");
    if (!std::regex_match(username, pattern)) {
        throw std::runtime_error(
            "Username must be 3 to 64 characters and use only letters, numbers, dots, underscores, hyphens, or @."
        );
    }
    return username;
}

std::string normalize_email(std::string email) {
    email = to_lower_copy(trim(std::move(email)));
    if (email.empty()) {
        throw std::runtime_error("Email cannot be empty.");
    }
    static const std::regex pattern(R"(^[^@\s]+@[^@\s]+\.[^@\s]+$)");
    if (!std::regex_match(email, pattern)) {
        throw std::runtime_error("Enter a valid email address.");
    }
    return email;
}

void validate_password(const std::string& password) {
    std::vector<std::string> requirements;
    if (static_cast<int>(password.size()) < kMinPasswordLength) {
        requirements.push_back("be at least " + std::to_string(kMinPasswordLength) + " characters long");
    }
    if (std::none_of(password.begin(), password.end(), [](unsigned char ch) { return std::islower(ch) != 0; })) {
        requirements.push_back("include a lowercase letter");
    }
    if (std::none_of(password.begin(), password.end(), [](unsigned char ch) { return std::isupper(ch) != 0; })) {
        requirements.push_back("include an uppercase letter");
    }
    if (std::none_of(password.begin(), password.end(), [](unsigned char ch) { return std::isdigit(ch) != 0; })) {
        requirements.push_back("include a number");
    }
    if (std::none_of(password.begin(), password.end(), [](unsigned char ch) { return !std::isalnum(ch); })) {
        requirements.push_back("include a symbol");
    }
    if (!requirements.empty()) {
        std::string message = "Password must ";
        for (std::size_t index = 0; index < requirements.size(); ++index) {
            if (index > 0) {
                message += ", ";
            }
            message += requirements[index];
        }
        message += ".";
        throw std::runtime_error(message);
    }
}

std::string generate_password_salt() {
    static thread_local std::mt19937 generator(std::random_device{}());
    static const std::string alphabet =
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789./";
    std::uniform_int_distribution<std::size_t> distribution(0, alphabet.size() - 1);
    std::string salt;
    salt.reserve(16);
    for (int index = 0; index < 16; ++index) {
        salt.push_back(alphabet[distribution(generator)]);
    }
    return "$6$" + salt + "$";
}

std::string hash_password(const std::string& password) {
#if defined(__linux__)
    struct crypt_data data {};
    data.initialized = 0;
    const std::string salt = generate_password_salt();
    char* encrypted = crypt_r(password.c_str(), salt.c_str(), &data);
    if (encrypted == nullptr) {
        throw std::runtime_error("Could not hash the password for local storage.");
    }
    return std::string(encrypted);
#else
    (void)password;
    throw std::runtime_error("Password hashing is only supported on Linux builds.");
#endif
}

bool verify_password_hash(const std::string& password, const std::string& password_hash) {
#if defined(__linux__)
    struct crypt_data data {};
    data.initialized = 0;
    char* encrypted = crypt_r(password.c_str(), password_hash.c_str(), &data);
    return encrypted != nullptr && password_hash == encrypted;
#else
    (void)password;
    (void)password_hash;
    return false;
#endif
}

bool authenticate_admin(const Config& config, const std::string& username, const std::string& password) {
    const auto credentials = configured_admin_credentials(config);
    if (!credentials.has_value()) {
        return false;
    }
    return trim(username) == credentials->first && verify_password_hash(password, credentials->second);
}

void clear_all_sessions(const Config& config) {
    SqliteConnection connection(config.session_db_path);
    exec_sql(connection.get(), "DELETE FROM auth_sessions");
}

void write_local_credentials(
    const Config& config,
    const std::string& username,
    const std::string& password,
    const std::optional<std::string>& email
) {
    const std::string normalized_username = normalize_username(username);
    validate_password(password);
    const std::optional<std::string> normalized_email =
        email.has_value() && !trim(*email).empty() ? std::optional<std::string>(normalize_email(*email)) : std::nullopt;
    const std::string password_hash = hash_password(password);
    const std::string now = iso_utc_now();

    SqliteConnection connection(config.session_db_path);
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            R"sql(
                INSERT INTO admin_credentials (id, username, password_hash, email, created_at, updated_at)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    username = excluded.username,
                    password_hash = excluded.password_hash,
                    email = excluded.email,
                    updated_at = excluded.updated_at
            )sql",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }

    sqlite3_bind_text(statement, 1, normalized_username.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 2, password_hash.c_str(), -1, SQLITE_TRANSIENT);
    if (normalized_email.has_value()) {
        sqlite3_bind_text(statement, 3, normalized_email->c_str(), -1, SQLITE_TRANSIENT);
    } else {
        sqlite3_bind_null(statement, 3);
    }
    sqlite3_bind_text(statement, 4, now.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 5, now.c_str(), -1, SQLITE_TRANSIENT);

    if (sqlite3_step(statement) != SQLITE_DONE) {
        const std::string message = sqlite3_errmsg(connection.get());
        sqlite3_finalize(statement);
        throw std::runtime_error(message);
    }
    sqlite3_finalize(statement);
}

void setup_admin_credentials(
    const Config& config,
    const std::string& username,
    const std::string& password,
    const std::optional<std::string>& email
) {
    if (get_auth_status(config).configured) {
        throw std::runtime_error("An account is already configured. Please log in.");
    }
    if (!email.has_value() || trim(*email).empty()) {
        throw std::runtime_error("Email cannot be empty.");
    }
    write_local_credentials(config, username, password, email);
    clear_all_sessions(config);
}

void reset_admin_credentials(
    const Config& config,
    const std::string& username,
    const std::string& new_password
) {
    const auto expected_username = configured_admin_credentials(config);
    if (!expected_username.has_value() || trim(username) != expected_username->first) {
        throw std::runtime_error("Username is incorrect.");
    }
    write_local_credentials(config, expected_username->first, new_password, configured_admin_email(config));
    clear_all_sessions(config);
}

void purge_recovery_codes(sqlite3* database) {
    sqlite3_stmt* statement = nullptr;
    const std::string now = iso_utc_now();
    if (sqlite3_prepare_v2(
            database,
            "DELETE FROM admin_recovery_codes WHERE expires_at <= ? OR consumed_at IS NOT NULL",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(database));
    }
    sqlite3_bind_text(statement, 1, now.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(statement) != SQLITE_DONE) {
        const std::string message = sqlite3_errmsg(database);
        sqlite3_finalize(statement);
        throw std::runtime_error(message);
    }
    sqlite3_finalize(statement);
}

void delete_recovery_codes(sqlite3* database, const std::string& purpose, const std::string& email) {
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(
            database,
            "DELETE FROM admin_recovery_codes WHERE purpose = ? AND email = ?",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(database));
    }
    sqlite3_bind_text(statement, 1, purpose.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 2, email.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(statement) != SQLITE_DONE) {
        const std::string message = sqlite3_errmsg(database);
        sqlite3_finalize(statement);
        throw std::runtime_error(message);
    }
    sqlite3_finalize(statement);
}

std::string generate_recovery_code() {
    static thread_local std::mt19937 generator(std::random_device{}());
    std::uniform_int_distribution<int> distribution(0, 9);
    std::string code;
    code.reserve(kRecoveryCodeLength);
    for (int index = 0; index < kRecoveryCodeLength; ++index) {
        code.push_back(static_cast<char>('0' + distribution(generator)));
    }
    return code;
}

void store_recovery_code(
    const Config& config,
    const std::string& purpose,
    const std::string& email,
    const std::string& code
) {
    SqliteConnection connection(config.session_db_path);
    purge_recovery_codes(connection.get());
    delete_recovery_codes(connection.get(), purpose, email);

    const std::string created_at = iso_utc_now();
    const std::string expires_at = iso_utc_after_seconds(int_env("ATTENDANCE_RECOVERY_CODE_TTL_SECONDS", kRecoveryTtlSecondsDefault));
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            R"sql(
                INSERT INTO admin_recovery_codes (purpose, email, code_hash, created_at, expires_at, consumed_at)
                VALUES (?, ?, ?, ?, ?, NULL)
            )sql",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }
    sqlite3_bind_text(statement, 1, purpose.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 2, email.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 3, code.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 4, created_at.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 5, expires_at.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(statement) != SQLITE_DONE) {
        const std::string message = sqlite3_errmsg(connection.get());
        sqlite3_finalize(statement);
        throw std::runtime_error(message);
    }
    sqlite3_finalize(statement);
}

RecoveryCodeRecord latest_recovery_code(
    const Config& config,
    const std::string& purpose,
    const std::string& email
) {
    SqliteConnection connection(config.session_db_path);
    purge_recovery_codes(connection.get());

    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            R"sql(
                SELECT id, code_hash
                FROM admin_recovery_codes
                WHERE purpose = ? AND email = ?
                ORDER BY created_at DESC
                LIMIT 1
            )sql",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }
    sqlite3_bind_text(statement, 1, purpose.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 2, email.c_str(), -1, SQLITE_TRANSIENT);

    RecoveryCodeRecord record;
    if (sqlite3_step(statement) != SQLITE_ROW) {
        sqlite3_finalize(statement);
        throw std::runtime_error("Invalid or expired verification code.");
    }
    record.id = sqlite3_column_int(statement, 0);
    record.code = sqlite_column_text(statement, 1);
    sqlite3_finalize(statement);
    return record;
}

void consume_recovery_code(
    const Config& config,
    const std::string& purpose,
    const std::string& email,
    const std::string& code
) {
    const std::string normalized_email = normalize_email(email);
    const std::string normalized_code = trim(code);
    if (normalized_code.empty()) {
        throw std::runtime_error("Verification code is required.");
    }

    SqliteConnection connection(config.session_db_path);
    purge_recovery_codes(connection.get());

    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            R"sql(
                SELECT id, code_hash
                FROM admin_recovery_codes
                WHERE purpose = ? AND email = ?
                ORDER BY created_at DESC
                LIMIT 1
            )sql",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }
    sqlite3_bind_text(statement, 1, purpose.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 2, normalized_email.c_str(), -1, SQLITE_TRANSIENT);

    int row_id = 0;
    std::string stored_code;
    if (sqlite3_step(statement) == SQLITE_ROW) {
        row_id = sqlite3_column_int(statement, 0);
        stored_code = sqlite_column_text(statement, 1);
    }
    sqlite3_finalize(statement);

    if (row_id == 0 || stored_code != normalized_code) {
        throw std::runtime_error("Invalid or expired verification code.");
    }

    sqlite3_stmt* update_statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            "UPDATE admin_recovery_codes SET consumed_at = ? WHERE id = ?",
            -1,
            &update_statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }
    const std::string consumed_at = iso_utc_now();
    sqlite3_bind_text(update_statement, 1, consumed_at.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int(update_statement, 2, row_id);
    if (sqlite3_step(update_statement) != SQLITE_DONE) {
        const std::string message = sqlite3_errmsg(connection.get());
        sqlite3_finalize(update_statement);
        throw std::runtime_error(message);
    }
    sqlite3_finalize(update_statement);
}

SessionState create_session(const Config& config, const std::string& username) {
    SqliteConnection connection(config.session_db_path);
    purge_expired_sessions(connection.get());

    const SessionState session{
        random_session_id(),
        username,
        iso_utc_after_seconds(config.session_ttl_seconds),
    };
    const std::string created_at = iso_utc_now();

    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            "INSERT INTO auth_sessions (session_id, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }

    sqlite3_bind_text(statement, 1, session.session_id.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 2, session.username.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 3, created_at.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 4, session.expires_at.c_str(), -1, SQLITE_TRANSIENT);

    if (sqlite3_step(statement) != SQLITE_DONE) {
        std::string message = sqlite3_errmsg(connection.get());
        sqlite3_finalize(statement);
        throw std::runtime_error(message);
    }
    sqlite3_finalize(statement);

    return session;
}

std::optional<SessionState> get_session(const Config& config, const std::string& session_id) {
    SqliteConnection connection(config.session_db_path);
    purge_expired_sessions(connection.get());

    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            "SELECT session_id, username, expires_at FROM auth_sessions WHERE session_id = ?",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }

    sqlite3_bind_text(statement, 1, session_id.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(statement) != SQLITE_ROW) {
        sqlite3_finalize(statement);
        return std::nullopt;
    }

    SessionState session{
        sqlite_column_text(statement, 0),
        sqlite_column_text(statement, 1),
        sqlite_column_text(statement, 2),
    };
    sqlite3_finalize(statement);

    if (session.expires_at <= iso_utc_now()) {
        return std::nullopt;
    }

    session.expires_at = iso_utc_after_seconds(config.session_ttl_seconds);
    sqlite3_stmt* update_statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            "UPDATE auth_sessions SET expires_at = ? WHERE session_id = ?",
            -1,
            &update_statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }

    sqlite3_bind_text(update_statement, 1, session.expires_at.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(update_statement, 2, session.session_id.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(update_statement) != SQLITE_DONE) {
        std::string message = sqlite3_errmsg(connection.get());
        sqlite3_finalize(update_statement);
        throw std::runtime_error(message);
    }
    sqlite3_finalize(update_statement);
    return session;
}

void delete_session(const Config& config, const std::string& session_id) {
    SqliteConnection connection(config.session_db_path);
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            "DELETE FROM auth_sessions WHERE session_id = ?",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }
    sqlite3_bind_text(statement, 1, session_id.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(statement) != SQLITE_DONE) {
        std::string message = sqlite3_errmsg(connection.get());
        sqlite3_finalize(statement);
        throw std::runtime_error(message);
    }
    sqlite3_finalize(statement);
}

std::optional<std::string> extract_bearer_token(const HttpRequest& request) {
    auto header = request.headers.find("authorization");
    if (header != request.headers.end()) {
        const std::string value = trim(header->second);
        if (value.size() > 7 && to_lower_copy(value.substr(0, 7)) == "bearer ") {
            return trim(value.substr(7));
        }
    }
    header = request.headers.find("x-auth-token");
    if (header != request.headers.end()) {
        const std::string value = trim(header->second);
        if (!value.empty()) {
            return value;
        }
    }
    if (!request.query.empty()) {
        const auto query_values = parse_query_string(request.query);
        const auto token_iter = query_values.find("token");
        if (token_iter != query_values.end() && !token_iter->second.empty()) {
            return token_iter->second;
        }
    }
    return std::nullopt;
}

std::optional<SessionState> require_auth(const Config& config, const HttpRequest& request) {
    const auto token = extract_bearer_token(request);
    if (!token.has_value() || token->empty()) {
        return std::nullopt;
    }
    return get_session(config, *token);
}

std::optional<std::string> extract_json_string(const std::string& body, const std::string& key) {
    const std::regex pattern("\"" + key + "\"\\s*:\\s*\"((?:\\\\.|[^\"])*)\"");
    std::smatch match;
    if (!std::regex_search(body, match, pattern)) {
        return std::nullopt;
    }

    std::string value = match[1].str();
    std::string output;
    output.reserve(value.size());
    bool escaping = false;
    for (char ch : value) {
        if (escaping) {
            switch (ch) {
                case '\\':
                case '"':
                case '/':
                    output.push_back(ch);
                    break;
                case 'b':
                    output.push_back('\b');
                    break;
                case 'f':
                    output.push_back('\f');
                    break;
                case 'n':
                    output.push_back('\n');
                    break;
                case 'r':
                    output.push_back('\r');
                    break;
                case 't':
                    output.push_back('\t');
                    break;
                default:
                    output.push_back(ch);
                    break;
            }
            escaping = false;
            continue;
        }
        if (ch == '\\') {
            escaping = true;
            continue;
        }
        output.push_back(ch);
    }
    return output;
}

std::optional<double> extract_json_number(const std::string& body, const std::string& key) {
    const std::regex pattern("\"" + key + "\"\\s*:\\s*(-?\\d+(?:\\.\\d+)?)");
    std::smatch match;
    if (!std::regex_search(body, match, pattern)) {
        return std::nullopt;
    }
    try {
        return std::stod(match[1].str());
    } catch (const std::exception&) {
        return std::nullopt;
    }
}

std::optional<int> extract_json_int(const std::string& body, const std::string& key) {
    if (const auto value = extract_json_number(body, key)) {
        return static_cast<int>(*value);
    }
    return std::nullopt;
}

std::optional<std::string> header_parameter(const std::string& header_value, const std::string& key) {
    const std::regex pattern(key + R"(\s*=\s*(?:"([^"]+)"|([^;]+)))", std::regex::icase);
    std::smatch match;
    if (!std::regex_search(header_value, match, pattern)) {
        return std::nullopt;
    }
    if (match[1].matched) {
        return match[1].str();
    }
    return trim(match[2].str());
}

std::vector<MultipartPart> parse_multipart_form_data(const HttpRequest& request) {
    const auto content_type_iter = request.headers.find("content-type");
    if (content_type_iter == request.headers.end()) {
        throw std::runtime_error("Multipart upload requires a Content-Type header.");
    }

    const auto boundary = header_parameter(content_type_iter->second, "boundary");
    if (!boundary.has_value() || boundary->empty()) {
        throw std::runtime_error("Multipart upload boundary is missing.");
    }

    const std::string marker = "--" + *boundary;
    const std::string separator = "\r\n" + marker;
    const std::string& body = request.body;
    std::size_t cursor = 0;
    if (body.rfind(marker, 0) != 0) {
        throw std::runtime_error("Multipart request body is malformed.");
    }

    std::vector<MultipartPart> parts;
    while (cursor < body.size()) {
        if (body.compare(cursor, marker.size(), marker) != 0) {
            throw std::runtime_error("Multipart boundary mismatch.");
        }

        cursor += marker.size();
        if (body.compare(cursor, 2, "--") == 0) {
            break;
        }
        if (body.compare(cursor, 2, "\r\n") != 0) {
            throw std::runtime_error("Multipart delimiter is malformed.");
        }
        cursor += 2;

        const std::size_t headers_end = body.find("\r\n\r\n", cursor);
        if (headers_end == std::string::npos) {
            throw std::runtime_error("Multipart part headers are incomplete.");
        }

        MultipartPart part;
        std::istringstream header_stream(body.substr(cursor, headers_end - cursor));
        std::string header_line;
        while (std::getline(header_stream, header_line)) {
            if (!header_line.empty() && header_line.back() == '\r') {
                header_line.pop_back();
            }
            const std::size_t colon = header_line.find(':');
            if (colon == std::string::npos) {
                continue;
            }

            const std::string key = to_lower_copy(trim(header_line.substr(0, colon)));
            const std::string value = trim(header_line.substr(colon + 1));
            if (key == "content-type") {
                part.content_type = value;
            } else if (key == "content-disposition") {
                part.name = header_parameter(value, "name").value_or("");
                part.filename = header_parameter(value, "filename");
            }
        }

        cursor = headers_end + 4;
        const std::size_t next_boundary = body.find(separator, cursor);
        if (next_boundary == std::string::npos) {
            throw std::runtime_error("Multipart closing boundary is missing.");
        }
        part.body = body.substr(cursor, next_boundary - cursor);
        parts.push_back(std::move(part));
        cursor = next_boundary + 2;
    }

    return parts;
}

std::optional<std::string> multipart_text_field(
    const std::vector<MultipartPart>& parts,
    const std::string& name
) {
    const auto match = std::find_if(
        parts.begin(),
        parts.end(),
        [&name](const MultipartPart& part) {
            return part.name == name;
        }
    );
    if (match == parts.end()) {
        return std::nullopt;
    }
    return match->body;
}

bool parse_bool_text(const std::string& value, bool default_value) {
    const std::string normalized = to_lower_copy(trim(value));
    if (normalized.empty()) {
        return default_value;
    }
    if (normalized == "true" || normalized == "1" || normalized == "yes" || normalized == "on") {
        return true;
    }
    if (normalized == "false" || normalized == "0" || normalized == "no" || normalized == "off") {
        return false;
    }
    return default_value;
}

std::optional<WorkerRow> fetch_worker_by_id(const Config& config, int worker_id) {
    SqliteConnection connection(config.scalable_db_path);
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            "SELECT id, employee_code, name, created_at FROM workers WHERE id = ?",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }
    sqlite3_bind_int(statement, 1, worker_id);

    std::optional<WorkerRow> worker;
    if (sqlite3_step(statement) == SQLITE_ROW) {
        worker = WorkerRow{
            sqlite3_column_int(statement, 0),
            sqlite_column_text(statement, 1),
            sqlite_column_text(statement, 2),
            sqlite_column_text(statement, 3),
        };
    }
    sqlite3_finalize(statement);
    return worker;
}

WorkerRow upsert_worker(const Config& config, const std::string& employee_code, const std::string& name) {
    const std::string normalized_code = trim(employee_code);
    const std::string normalized_name = trim(name);
    if (normalized_code.empty()) {
        throw std::runtime_error("Employee code cannot be empty.");
    }
    if (normalized_name.empty()) {
        throw std::runtime_error("Name cannot be empty.");
    }

    SqliteConnection connection(config.scalable_db_path);
    sqlite3_stmt* statement = nullptr;
    const std::string created_at = iso_utc_now();
    if (sqlite3_prepare_v2(
            connection.get(),
            R"sql(
                INSERT INTO workers (employee_code, name, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(employee_code) DO UPDATE SET name = excluded.name
            )sql",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }
    sqlite3_bind_text(statement, 1, normalized_code.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 2, normalized_name.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 3, created_at.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(statement) != SQLITE_DONE) {
        const std::string message = sqlite3_errmsg(connection.get());
        sqlite3_finalize(statement);
        throw std::runtime_error(message);
    }
    sqlite3_finalize(statement);

    sqlite3_stmt* fetch_statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            "SELECT id, employee_code, name, created_at FROM workers WHERE employee_code = ?",
            -1,
            &fetch_statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }
    sqlite3_bind_text(fetch_statement, 1, normalized_code.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(fetch_statement) != SQLITE_ROW) {
        sqlite3_finalize(fetch_statement);
        throw std::runtime_error("Worker upsert failed.");
    }
    WorkerRow worker{
        sqlite3_column_int(fetch_statement, 0),
        sqlite_column_text(fetch_statement, 1),
        sqlite_column_text(fetch_statement, 2),
        sqlite_column_text(fetch_statement, 3),
    };
    sqlite3_finalize(fetch_statement);
    return worker;
}

void delete_embeddings_for_worker(const Config& config, int worker_id) {
    SqliteConnection connection(config.scalable_db_path);
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            "DELETE FROM worker_embeddings WHERE worker_id = ?",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }
    sqlite3_bind_int(statement, 1, worker_id);
    if (sqlite3_step(statement) != SQLITE_DONE) {
        const std::string message = sqlite3_errmsg(connection.get());
        sqlite3_finalize(statement);
        throw std::runtime_error(message);
    }
    sqlite3_finalize(statement);
}

void store_embedding(const Config& config, int worker_id, const std::vector<float>& embedding) {
    SqliteConnection connection(config.scalable_db_path);
    sqlite3_stmt* statement = nullptr;
    const std::string created_at = iso_utc_now();
    if (sqlite3_prepare_v2(
            connection.get(),
            R"sql(
                INSERT INTO worker_embeddings (worker_id, backend, dimension, face_image, vector, created_at)
                VALUES (?, ?, ?, NULL, ?, ?)
            )sql",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }
    sqlite3_bind_int(statement, 1, worker_id);
    sqlite3_bind_text(statement, 2, kNativeEmbedderName, -1, SQLITE_TRANSIENT);
    sqlite3_bind_int(statement, 3, static_cast<int>(embedding.size()));
    sqlite3_bind_blob(
        statement,
        4,
        embedding.data(),
        static_cast<int>(embedding.size() * sizeof(float)),
        SQLITE_TRANSIENT
    );
    sqlite3_bind_text(statement, 5, created_at.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(statement) != SQLITE_DONE) {
        const std::string message = sqlite3_errmsg(connection.get());
        sqlite3_finalize(statement);
        throw std::runtime_error(message);
    }
    sqlite3_finalize(statement);
}

int attendance_cooldown_hours() {
    return int_env("ATTENDANCE_COOLDOWN_HOURS", kAttendanceCooldownHoursDefault);
}

bool recent_attendance_exists(sqlite3* database, int worker_id) {
    sqlite3_stmt* statement = nullptr;
    const auto cutoff_time = std::chrono::system_clock::now() - std::chrono::hours(attendance_cooldown_hours());
    const std::time_t cutoff = std::chrono::system_clock::to_time_t(cutoff_time);
    std::tm utc_tm{};
#if defined(_WIN32)
    gmtime_s(&utc_tm, &cutoff);
#else
    gmtime_r(&cutoff, &utc_tm);
#endif
    std::ostringstream cutoff_stream;
    cutoff_stream << std::put_time(&utc_tm, "%Y-%m-%dT%H:%M:%S+00:00");
    const std::string cutoff_iso = cutoff_stream.str();

    if (sqlite3_prepare_v2(
            database,
            R"sql(
                SELECT id FROM attendance_events
                WHERE worker_id = ? AND attendance_marked = 1 AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT 1
            )sql",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(database));
    }
    sqlite3_bind_int(statement, 1, worker_id);
    sqlite3_bind_text(statement, 2, cutoff_iso.c_str(), -1, SQLITE_TRANSIENT);
    const bool exists = sqlite3_step(statement) == SQLITE_ROW;
    sqlite3_finalize(statement);
    return exists;
}

AttendanceEventRecord record_screening_event(
    const Config& config,
    int worker_id,
    const std::string& camera_id,
    double matched_score,
    const BreathReading& reading
) {
    SqliteConnection connection(config.scalable_db_path);
    exec_sql(connection.get(), "BEGIN IMMEDIATE");

    const bool overall_clear = reading.alcohol_clear && reading.cannabis_clear;
    bool attendance_marked = false;
    if (overall_clear) {
        attendance_marked = !recent_attendance_exists(connection.get(), worker_id);
    }

    sqlite3_stmt* statement = nullptr;
    const std::string created_at = iso_utc_now();
    if (sqlite3_prepare_v2(
            connection.get(),
            R"sql(
                INSERT INTO attendance_events (
                    worker_id,
                    camera_id,
                    matched_score,
                    raw_sensor_value,
                    alcohol_ppb,
                    cannabis_ppb,
                    alcohol_clear,
                    cannabis_clear,
                    attendance_marked,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            )sql",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        exec_sql(connection.get(), "ROLLBACK");
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }
    sqlite3_bind_int(statement, 1, worker_id);
    sqlite3_bind_text(statement, 2, camera_id.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_double(statement, 3, matched_score);
    if (reading.raw_sensor_value.has_value()) {
        sqlite3_bind_double(statement, 4, *reading.raw_sensor_value);
    } else {
        sqlite3_bind_null(statement, 4);
    }
    sqlite3_bind_double(statement, 5, reading.alcohol_ppb);
    sqlite3_bind_double(statement, 6, reading.cannabis_ppb);
    sqlite3_bind_int(statement, 7, reading.alcohol_clear ? 1 : 0);
    sqlite3_bind_int(statement, 8, reading.cannabis_clear ? 1 : 0);
    sqlite3_bind_int(statement, 9, attendance_marked ? 1 : 0);
    sqlite3_bind_text(statement, 10, created_at.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(statement) != SQLITE_DONE) {
        const std::string message = sqlite3_errmsg(connection.get());
        sqlite3_finalize(statement);
        exec_sql(connection.get(), "ROLLBACK");
        throw std::runtime_error(message);
    }
    const int event_id = static_cast<int>(sqlite3_last_insert_rowid(connection.get()));
    sqlite3_finalize(statement);
    exec_sql(connection.get(), "COMMIT");

    return AttendanceEventRecord{
        event_id,
        worker_id,
        camera_id,
        matched_score,
        reading.raw_sensor_value,
        reading.alcohol_ppb,
        reading.cannabis_ppb,
        reading.alcohol_clear,
        reading.cannabis_clear,
        attendance_marked,
        created_at,
    };
}

bool future_ready(std::future<BreathReading>& future) {
    return future.wait_for(std::chrono::seconds(0)) == std::future_status::ready;
}

void prune_breath_sessions_locked() {
    auto& sessions = breath_sessions();
    for (auto iter = sessions.begin(); iter != sessions.end();) {
        if (iter->second.completed || (iter->second.canceled && future_ready(iter->second.future))) {
            iter = sessions.erase(iter);
        } else {
            ++iter;
        }
    }
}

PendingBreathSession* active_breath_session_locked() {
    auto& sessions = breath_sessions();
    for (auto& [session_id, session] : sessions) {
        (void)session_id;
        if (!future_ready(session.future)) {
            return &session;
        }
        if (!session.completed && !session.canceled) {
            return &session;
        }
    }
    return nullptr;
}

std::string recognition_result_json(const RecognitionResult& result) {
    std::ostringstream body;
    body << "{"
         << "\"matches\":[";
    for (std::size_t index = 0; index < result.matches.size(); ++index) {
        if (index > 0) {
            body << ",";
        }
        const RecognitionMatch& match = result.matches[index];
        body << "{"
             << "\"worker_id\":" << match.worker_id << ","
             << "\"employee_code\":" << json_string(match.employee_code) << ","
             << "\"name\":" << json_string(match.name) << ","
             << "\"score\":" << match.score << ","
             << "\"attendance_marked\":" << bool_json(match.attendance_marked) << ","
             << "\"source\":" << json_string(match.source)
             << "}";
    }
    body << "],"
         << "\"unknown_faces\":" << result.unknown_faces << ","
         << "\"detected_faces\":" << result.detected_faces << ","
         << "\"boxes\":[";
    for (std::size_t index = 0; index < result.boxes.size(); ++index) {
        if (index > 0) {
            body << ",";
        }
        const DetectionBox& box = result.boxes[index];
        body << "{"
             << "\"x\":" << box.x << ","
             << "\"y\":" << box.y << ","
             << "\"width\":" << box.width << ","
             << "\"height\":" << box.height
             << "}";
    }
    body << "],"
         << "\"debug_faces\":[";
    for (std::size_t index = 0; index < result.debug_faces.size(); ++index) {
        if (index > 0) {
            body << ",";
        }
        const FaceDebug& face = result.debug_faces[index];
        body << "{"
             << "\"face_index\":" << face.face_index << ","
             << "\"accepted\":" << bool_json(face.accepted) << ","
             << "\"reason\":" << json_string(face.reason) << ",";
        if (face.blur_variance.has_value()) {
            body << "\"blur_variance\":" << *face.blur_variance << ",";
        } else {
            body << "\"blur_variance\":null,";
        }
        if (face.brightness.has_value()) {
            body << "\"brightness\":" << *face.brightness << ",";
        } else {
            body << "\"brightness\":null,";
        }
        if (face.eyes_detected.has_value()) {
            body << "\"eyes_detected\":" << *face.eyes_detected << ",";
        } else {
            body << "\"eyes_detected\":null,";
        }
        body << "\"candidates\":[";
        for (std::size_t candidate_index = 0; candidate_index < face.candidates.size(); ++candidate_index) {
            if (candidate_index > 0) {
                body << ",";
            }
            const CandidateDebug& candidate = face.candidates[candidate_index];
            body << "{"
                 << "\"worker_id\":" << candidate.worker_id << ","
                 << "\"score\":" << candidate.score
                 << "}";
        }
        body << "]"
             << "}";
    }
    body << "]"
         << "}";
    return body.str();
}

std::string worker_json(const WorkerRow& worker) {
    std::ostringstream body;
    body << "{"
         << "\"id\":" << worker.id << ","
         << "\"employee_code\":" << json_string(worker.employee_code) << ","
         << "\"name\":" << json_string(worker.name) << ","
         << "\"created_at\":" << json_string(worker.created_at)
         << "}";
    return body.str();
}

std::string breath_test_result_json(
    const WorkerRow& worker,
    double matched_score,
    const AttendanceEventRecord& event
) {
    std::ostringstream body;
    body << "{"
         << "\"worker_id\":" << worker.id << ","
         << "\"employee_code\":" << json_string(worker.employee_code) << ","
         << "\"name\":" << json_string(worker.name) << ","
         << "\"matched_score\":" << matched_score << ",";
    if (event.raw_sensor_value.has_value()) {
        body << "\"raw_sensor_value\":" << *event.raw_sensor_value << ",";
    } else {
        body << "\"raw_sensor_value\":null,";
    }
    body << "\"alcohol_ppb\":" << event.alcohol_ppb << ","
         << "\"cannabis_ppb\":" << event.cannabis_ppb << ","
         << "\"alcohol_clear\":" << bool_json(event.alcohol_clear) << ","
         << "\"cannabis_clear\":" << bool_json(event.cannabis_clear) << ","
         << "\"overall_clear\":" << bool_json(event.alcohol_clear && event.cannabis_clear) << ","
         << "\"attendance_marked\":" << bool_json(event.attendance_marked) << ","
         << "\"created_at\":" << json_string(event.created_at)
         << "}";
    return body.str();
}

HttpResponse make_response(int status, const std::string& content_type, std::string body) {
    HttpResponse response;
    response.status = status;
    response.body = std::move(body);
    response.content_type = content_type;
    return response;
}

HttpResponse make_json_response(int status, const std::string& body) {
    return make_response(status, "application/json", body);
}

HttpResponse error_response(int status, const std::string& detail) {
    return make_json_response(status, "{\"detail\":" + json_string(detail) + "}");
}

std::string warnings_json(const std::vector<std::string>& warnings) {
    std::ostringstream body;
    body << "[";
    for (std::size_t index = 0; index < warnings.size(); ++index) {
        if (index > 0) {
            body << ",";
        }
        body << json_string(warnings[index]);
    }
    body << "]";
    return body.str();
}

HttpResponse auth_status_response(const Config& config) {
    const AuthStatus status = get_auth_status(config);
    std::ostringstream body;
    body << "{"
         << "\"configured\":" << bool_json(status.configured) << ","
         << "\"setup_required\":" << bool_json(status.setup_required) << ","
         << "\"source\":" << json_string(status.source) << ","
         << "\"email_configured\":" << bool_json(status.email_configured) << ","
         << "\"email_recovery_enabled\":" << bool_json(status.email_recovery_enabled)
         << "}";
    return make_json_response(200, body.str());
}

HttpResponse login_response(const Config& config, const HttpRequest& request) {
    const auto username = extract_json_string(request.body, "username");
    const auto password = extract_json_string(request.body, "password");
    if (!username.has_value() || !password.has_value()) {
        return error_response(400, "Username and password are required.");
    }
    if (!authenticate_admin(config, *username, *password)) {
        return error_response(401, "Invalid username or password.");
    }

    const SessionState session = create_session(config, trim(*username));
    std::ostringstream body;
    body << "{"
         << "\"token\":" << json_string(session.session_id) << ","
         << "\"username\":" << json_string(session.username) << ","
         << "\"expires_at\":" << json_string(session.expires_at)
         << "}";
    return make_json_response(200, body.str());
}

HttpResponse setup_response(const Config& config, const HttpRequest& request) {
    const auto username = extract_json_string(request.body, "username");
    const auto email = extract_json_string(request.body, "email");
    const auto password = extract_json_string(request.body, "password");
    const auto confirm_password = extract_json_string(request.body, "confirm_password");
    if (!username.has_value() || !email.has_value() || !password.has_value() || !confirm_password.has_value()) {
        return error_response(400, "Username, email, password, and confirmation are required.");
    }
    if (*password != *confirm_password) {
        return error_response(400, "Passwords do not match.");
    }

    try {
        setup_admin_credentials(config, *username, *password, *email);
        const SessionState session = create_session(config, normalize_username(*username));
        std::ostringstream body;
        body << "{"
             << "\"token\":" << json_string(session.session_id) << ","
             << "\"username\":" << json_string(session.username) << ","
             << "\"expires_at\":" << json_string(session.expires_at)
             << "}";
        return make_json_response(200, body.str());
    } catch (const std::exception& exc) {
        return error_response(400, exc.what());
    }
}

HttpResponse reset_response(const Config& config, const HttpRequest& request) {
    const auto username = extract_json_string(request.body, "username");
    const auto new_password = extract_json_string(request.body, "new_password");
    const auto confirm_password = extract_json_string(request.body, "confirm_password");
    if (!username.has_value() || !new_password.has_value() || !confirm_password.has_value()) {
        return error_response(400, "Username, new password, and confirmation are required.");
    }
    if (*new_password != *confirm_password) {
        return error_response(400, "Passwords do not match.");
    }

    try {
        reset_admin_credentials(config, *username, *new_password);
        std::ostringstream body;
        body << "{"
             << "\"ok\":true,"
             << "\"username\":" << json_string(trim(*username)) << ","
             << "\"message\":"
             << json_string("Password reset successful. Please log in with your username and new password.")
             << "}";
        return make_json_response(200, body.str());
    } catch (const std::exception& exc) {
        return error_response(400, exc.what());
    }
}

HttpResponse recovery_request_response(
    const Config& config,
    const HttpRequest& request,
    const std::string& purpose
) {
    const auto email = extract_json_string(request.body, "email");
    if (!email.has_value()) {
        return error_response(400, "Email is required.");
    }

    try {
        const auto expected_email = configured_admin_email(config);
        if (!expected_email.has_value()) {
            return error_response(400, "No recovery email is registered for this login.");
        }
        const std::string normalized_email = normalize_email(*email);
        const std::string message = "If the email matches the registered email, a verification code has been sent.";
        if (normalized_email != *expected_email) {
            return make_json_response(200, "{\"ok\":true,\"message\":" + json_string(message) + "}");
        }

        const std::string code = generate_recovery_code();
        store_recovery_code(config, purpose, normalized_email, code);
        const std::string delivery =
            message + " Device verification code: " + code + ".";
        return make_json_response(200, "{\"ok\":true,\"message\":" + json_string(delivery) + "}");
    } catch (const std::exception& exc) {
        return error_response(400, exc.what());
    }
}

HttpResponse verify_username_recovery_response(const Config& config, const HttpRequest& request) {
    const auto email = extract_json_string(request.body, "email");
    const auto code = extract_json_string(request.body, "code");
    if (!email.has_value() || !code.has_value()) {
        return error_response(400, "Email and verification code are required.");
    }

    try {
        consume_recovery_code(config, "username", *email, *code);
        const auto credentials = configured_admin_credentials(config);
        if (!credentials.has_value()) {
            throw std::runtime_error("Username is not configured.");
        }
        std::ostringstream body;
        body << "{"
             << "\"ok\":true,"
             << "\"username\":" << json_string(credentials->first) << ","
             << "\"message\":" << json_string("Username verified.")
             << "}";
        return make_json_response(200, body.str());
    } catch (const std::exception& exc) {
        return error_response(400, exc.what());
    }
}

HttpResponse verify_password_recovery_response(const Config& config, const HttpRequest& request) {
    const auto email = extract_json_string(request.body, "email");
    const auto code = extract_json_string(request.body, "code");
    const auto new_password = extract_json_string(request.body, "new_password");
    const auto confirm_password = extract_json_string(request.body, "confirm_password");
    if (!email.has_value() || !code.has_value() || !new_password.has_value() || !confirm_password.has_value()) {
        return error_response(400, "Email, verification code, and password confirmation are required.");
    }
    if (*new_password != *confirm_password) {
        return error_response(400, "Passwords do not match.");
    }

    try {
        consume_recovery_code(config, "password", *email, *code);
        const auto credentials = configured_admin_credentials(config);
        if (!credentials.has_value()) {
            throw std::runtime_error("Username is not configured.");
        }
        write_local_credentials(config, credentials->first, *new_password, normalize_email(*email));
        clear_all_sessions(config);
        return make_json_response(
            200,
            "{\"ok\":true,\"message\":" +
                json_string("Password reset successful. Please log in with your new password.") + "}"
        );
    } catch (const std::exception& exc) {
        return error_response(400, exc.what());
    }
}

HttpResponse me_response(const SessionState& session) {
    std::ostringstream body;
    body << "{"
         << "\"username\":" << json_string(session.username) << ","
         << "\"expires_at\":" << json_string(session.expires_at)
         << "}";
    return make_json_response(200, body.str());
}

HttpResponse logout_response(const Config& config, const HttpRequest& request) {
    if (const auto token = extract_bearer_token(request)) {
        if (!token->empty()) {
            delete_session(config, *token);
        }
    }
    return make_json_response(200, "{\"ok\":true}");
}

int count_rows(sqlite3* database, const std::string& query) {
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(database, query.c_str(), -1, &statement, nullptr) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(database));
    }
    int count = 0;
    if (sqlite3_step(statement) == SQLITE_ROW) {
        count = sqlite3_column_int(statement, 0);
    }
    sqlite3_finalize(statement);
    return count;
}

HttpResponse service_status_response(const Config& config) {
    SqliteConnection connection(config.scalable_db_path);
    const int indexed_workers = count_rows(connection.get(), "SELECT COUNT(*) FROM workers");
    const int indexed_embeddings = count_rows(connection.get(), "SELECT COUNT(*) FROM worker_embeddings");
    const int attendance_events = count_rows(connection.get(), "SELECT COUNT(*) FROM attendance_events");
    std::vector<std::string> warnings = breath_analyzer().startup_warnings();
    if (breath_analyzer().name() == "mock") {
        warnings.push_back(
            "Breath analyzer readings are running in mock mode. Configure the SPI board variables to enable live sensor reads."
        );
    }

    std::ostringstream body;
    body << "{"
         << "\"indexed_workers\":" << indexed_workers << ","
         << "\"indexed_embeddings\":" << indexed_embeddings << ","
         << "\"attendance_events\":" << attendance_events << ","
         << "\"cache_entries\":0,"
         << "\"active_detector\":" << json_string(face_detector().backend_name()) << ","
         << "\"requested_embedder\":" << json_string(config.requested_embedder) << ","
         << "\"active_embedder\":" << json_string(kNativeEmbedderName) << ","
         << "\"requested_index\":" << json_string(config.requested_index) << ","
         << "\"active_index\":" << json_string(kNativeIndexName) << ","
         << "\"fallback_enabled\":" << bool_json(config.fallback_enabled) << ","
         << "\"warnings\":" << warnings_json(warnings)
         << "}";
    return make_json_response(200, body.str());
}

HttpResponse architecture_response(const Config& config) {
    std::vector<std::string> warnings = breath_analyzer().startup_warnings();
    if (breath_analyzer().name() == "mock") {
        warnings.push_back(
            "Breath analyzer readings are running in mock mode. Configure the SPI board variables to enable live sensor reads."
        );
    }
    std::ostringstream body;
    body << "{"
         << "\"detector\":" << json_string(face_detector().backend_name()) << ","
         << "\"embedder\":" << json_string("Native C++ face descriptor pipeline") << ","
         << "\"index\":" << json_string("SQLite-backed on-demand descriptor lookup") << ","
         << "\"production_upgrade\":"
         << json_string("The Linux app now runs on the native backend path; future upgrades can swap in a stronger descriptor model without changing the frontend contract.") << ","
         << "\"requested_embedder\":" << json_string(config.requested_embedder) << ","
         << "\"active_embedder\":" << json_string(kNativeEmbedderName) << ","
         << "\"requested_index\":" << json_string(config.requested_index) << ","
         << "\"active_index\":" << json_string(kNativeIndexName) << ","
         << "\"fallback_enabled\":" << bool_json(config.fallback_enabled) << ","
         << "\"warnings\":" << warnings_json(warnings)
         << "}";
    return make_json_response(200, body.str());
}

HttpResponse local_camera_status_response() {
    const std::string source_name = camera_runtime().source_name();
    std::ostringstream body;
    body << "{"
         << "\"ok\":true,"
         << "\"running\":" << bool_json(camera_runtime().is_running()) << ","
         << "\"mode\":" << json_string("backend") << ","
         << "\"source_name\":" << json_string(source_name) << ","
         << "\"frame_path\":" << json_string("/api/v2/local-camera/frame")
         << "}";
    return make_json_response(200, body.str());
}

HttpResponse start_local_camera_response() {
    try {
        const std::string source_name = camera_runtime().start();
        std::ostringstream body;
        body << "{"
             << "\"ok\":true,"
             << "\"running\":true,"
             << "\"mode\":" << json_string("backend") << ","
             << "\"source_name\":" << json_string(source_name) << ","
             << "\"frame_path\":" << json_string("/api/v2/local-camera/frame")
             << "}";
        return make_json_response(200, body.str());
    } catch (const std::exception& exc) {
        return error_response(400, exc.what());
    }
}

HttpResponse stop_local_camera_response() {
    camera_runtime().stop();
    return make_json_response(200, "{\"ok\":true,\"running\":false}");
}

HttpResponse local_camera_frame_response() {
    try {
        if (!camera_runtime().is_running()) {
            camera_runtime().start();
        }
        const std::vector<unsigned char> frame_bytes = camera_runtime().get_frame_bytes();
        HttpResponse response = make_response(
            200,
            "image/jpeg",
            std::string(reinterpret_cast<const char*>(frame_bytes.data()), frame_bytes.size())
        );
        response.headers["Pragma"] = "no-cache";
        response.headers["Expires"] = "0";
        return response;
    } catch (const std::exception& exc) {
        return error_response(503, exc.what());
    }
}

HttpResponse detect_response(const HttpRequest& request) {
    try {
        const std::vector<MultipartPart> parts = parse_multipart_form_data(request);
        const auto image_part = std::find_if(
            parts.begin(),
            parts.end(),
            [](const MultipartPart& part) {
                return part.name == "image" && !part.body.empty();
            }
        );
        if (image_part == parts.end()) {
            return error_response(400, "Detection requires an uploaded image.");
        }

        const std::vector<unsigned char> encoded(image_part->body.begin(), image_part->body.end());
        const cv::Mat image = cv::imdecode(encoded, cv::IMREAD_COLOR);
        if (image.empty()) {
            return error_response(400, "Could not decode the uploaded image.");
        }

        const std::vector<DetectionBox> boxes = face_detector().detect(image);
        std::ostringstream body;
        body << "{"
             << "\"detected_faces\":" << boxes.size() << ","
             << "\"boxes\":[";
        for (std::size_t index = 0; index < boxes.size(); ++index) {
            if (index > 0) {
                body << ",";
            }
            body << "{"
                 << "\"x\":" << boxes[index].x << ","
                 << "\"y\":" << boxes[index].y << ","
                 << "\"width\":" << boxes[index].width << ","
                 << "\"height\":" << boxes[index].height
                 << "}";
        }
        body << "],"
             << "\"detector_backend\":" << json_string(face_detector().backend_name())
             << "}";
        return make_json_response(200, body.str());
    } catch (const std::exception& exc) {
        return error_response(400, exc.what());
    }
}

HttpResponse recognize_response(const Config& config, const HttpRequest& request) {
    try {
        const std::vector<MultipartPart> parts = parse_multipart_form_data(request);
        const auto image_part = std::find_if(
            parts.begin(),
            parts.end(),
            [](const MultipartPart& part) {
                return part.name == "image" && !part.body.empty();
            }
        );
        if (image_part == parts.end()) {
            return error_response(400, "Recognition requires an uploaded image.");
        }

        const std::string camera_id = multipart_text_field(parts, "camera_id").value_or("device-front-camera");
        int top_k = 3;
        if (const auto top_k_field = multipart_text_field(parts, "top_k")) {
            try {
                top_k = std::stoi(trim(*top_k_field));
            } catch (const std::exception&) {
                top_k = 3;
            }
        }

        const std::vector<unsigned char> encoded(image_part->body.begin(), image_part->body.end());
        const RecognitionResult result = native_recognizer().recognize(
            config.scalable_db_path,
            encoded,
            camera_id,
            top_k
        );
        return make_json_response(200, recognition_result_json(result));
    } catch (const std::exception& exc) {
        return error_response(400, exc.what());
    }
}

HttpResponse enroll_worker_response(const Config& config, const HttpRequest& request) {
    try {
        const std::vector<MultipartPart> parts = parse_multipart_form_data(request);
        const auto employee_code = multipart_text_field(parts, "employee_code");
        const auto name = multipart_text_field(parts, "name");
        if (!employee_code.has_value() || !name.has_value()) {
            return error_response(400, "Employee code and name are required.");
        }

        const bool replace_existing = parse_bool_text(
            multipart_text_field(parts, "replace_existing").value_or("true"),
            true
        );

        std::vector<std::vector<unsigned char>> image_payloads;
        for (const MultipartPart& part : parts) {
            if (part.name == "images" && !part.body.empty()) {
                image_payloads.emplace_back(part.body.begin(), part.body.end());
            }
        }

        const int minimum_images = int_env("ATTENDANCE_MIN_ENROLLMENT_IMAGES", kMinEnrollmentImagesDefault);
        if (static_cast<int>(image_payloads.size()) < minimum_images) {
            return error_response(400, "Enrollment requires at least " + std::to_string(minimum_images) + " face images.");
        }

        std::vector<std::vector<float>> embeddings;
        embeddings.reserve(image_payloads.size());
        for (const auto& image_bytes : image_payloads) {
            embeddings.push_back(native_recognizer().prepare_enrollment_embedding(image_bytes));
        }

        const WorkerRow worker = upsert_worker(config, *employee_code, *name);
        if (replace_existing) {
            delete_embeddings_for_worker(config, worker.id);
        }
        for (const auto& embedding : embeddings) {
            store_embedding(config, worker.id, embedding);
        }

        SqliteConnection connection(config.scalable_db_path);
        const int index_size = count_rows(connection.get(), "SELECT COUNT(*) FROM worker_embeddings");
        std::ostringstream body;
        body << "{"
             << "\"worker\":" << worker_json(worker) << ","
             << "\"embeddings_added\":" << embeddings.size() << ","
             << "\"index_size\":" << index_size
             << "}";
        return make_json_response(200, body.str());
    } catch (const std::exception& exc) {
        return error_response(400, exc.what());
    }
}

HttpResponse run_breath_test_response(const Config& config, const HttpRequest& request) {
    const auto worker_id = extract_json_int(request.body, "worker_id");
    const auto camera_id = extract_json_string(request.body, "camera_id");
    const auto matched_score = extract_json_number(request.body, "matched_score");
    if (!worker_id.has_value() || !camera_id.has_value() || !matched_score.has_value()) {
        return error_response(400, "worker_id, camera_id, and matched_score are required.");
    }

    try {
        const auto worker = fetch_worker_by_id(config, *worker_id);
        if (!worker.has_value()) {
            return error_response(400, "No worker found for id '" + std::to_string(*worker_id) + "'.");
        }
        const BreathReading reading = breath_analyzer().read(*worker_id, *camera_id);
        const AttendanceEventRecord event = record_screening_event(config, *worker_id, *camera_id, *matched_score, reading);
        return make_json_response(200, breath_test_result_json(*worker, *matched_score, event));
    } catch (const std::exception& exc) {
        return error_response(400, exc.what());
    }
}

HttpResponse start_breath_test_response(const Config& config, const HttpRequest& request) {
    const auto worker_id = extract_json_int(request.body, "worker_id");
    const auto camera_id = extract_json_string(request.body, "camera_id");
    if (!worker_id.has_value() || !camera_id.has_value()) {
        return error_response(400, "worker_id and camera_id are required.");
    }

    try {
        const auto worker = fetch_worker_by_id(config, *worker_id);
        if (!worker.has_value()) {
            return error_response(400, "No worker found for id '" + std::to_string(*worker_id) + "'.");
        }

        std::lock_guard<std::mutex> lock(breath_session_mutex());
        prune_breath_sessions_locked();
        if (active_breath_session_locked() != nullptr) {
            return error_response(400, "A breath test is already active on this device. Finish or cancel it before starting another.");
        }

        const std::string session_id = random_session_id();
        const std::string started_at = iso_utc_now();
        PendingBreathSession session;
        session.session_id = session_id;
        session.worker_id = *worker_id;
        session.camera_id = *camera_id;
        session.started_at = started_at;
        session.sample_seconds = std::max(1.0, breath_analyzer().sample_seconds());
        session.future = std::async(
            std::launch::async,
            [worker_id = *worker_id, camera_id = *camera_id]() {
                return breath_analyzer().read(worker_id, camera_id);
            }
        );
        breath_sessions().emplace(session_id, std::move(session));

        std::ostringstream body;
        body << "{"
             << "\"session_id\":" << json_string(session_id) << ","
             << "\"worker_id\":" << *worker_id << ","
             << "\"camera_id\":" << json_string(*camera_id) << ","
             << "\"sample_seconds\":" << std::max(1.0, breath_analyzer().sample_seconds()) << ","
             << "\"started_at\":" << json_string(started_at)
             << "}";
        return make_json_response(200, body.str());
    } catch (const std::exception& exc) {
        return error_response(400, exc.what());
    }
}

HttpResponse complete_breath_test_response(const Config& config, const HttpRequest& request) {
    const auto session_id = extract_json_string(request.body, "session_id");
    const auto matched_score = extract_json_number(request.body, "matched_score");
    if (!session_id.has_value() || !matched_score.has_value()) {
        return error_response(400, "session_id and matched_score are required.");
    }

    try {
        std::lock_guard<std::mutex> lock(breath_session_mutex());
        auto session_iter = breath_sessions().find(*session_id);
        if (session_iter == breath_sessions().end()) {
            return error_response(400, "The requested breath test session was not found.");
        }
        PendingBreathSession& session = session_iter->second;
        if (session.canceled) {
            return error_response(400, "The breath test session was canceled before completion.");
        }

        const auto worker = fetch_worker_by_id(config, session.worker_id);
        if (!worker.has_value()) {
            return error_response(400, "No worker found for id '" + std::to_string(session.worker_id) + "'.");
        }

        const auto timeout = std::chrono::duration<double>(std::max(5.0, session.sample_seconds + 5.0));
        if (session.future.wait_for(timeout) != std::future_status::ready) {
            return error_response(400, "The breath sensor is still processing this exhale. Please wait a moment and try again.");
        }

        const BreathReading reading = session.future.get();
        const AttendanceEventRecord event = record_screening_event(
            config,
            session.worker_id,
            session.camera_id,
            *matched_score,
            reading
        );
        session.completed = true;
        prune_breath_sessions_locked();
        return make_json_response(200, breath_test_result_json(*worker, *matched_score, event));
    } catch (const std::exception& exc) {
        return error_response(400, exc.what());
    }
}

HttpResponse cancel_breath_test_response(const std::string& session_id) {
    std::lock_guard<std::mutex> lock(breath_session_mutex());
    auto session_iter = breath_sessions().find(session_id);
    if (session_iter == breath_sessions().end()) {
        return error_response(400, "The requested breath test session was not found.");
    }

    session_iter->second.canceled = true;
    if (future_ready(session_iter->second.future)) {
        prune_breath_sessions_locked();
    }
    return make_json_response(
        200,
        "{\"session_id\":" + json_string(session_id) + ",\"canceled\":true}"
    );
}

HttpResponse rebuild_index_response(const Config& config) {
    SqliteConnection connection(config.scalable_db_path);
    std::ostringstream body;
    body << "{"
         << "\"indexed_workers\":" << count_rows(connection.get(), "SELECT COUNT(*) FROM workers") << ","
         << "\"indexed_embeddings\":" << count_rows(connection.get(), "SELECT COUNT(*) FROM worker_embeddings")
         << "}";
    return make_json_response(200, body.str());
}

HttpResponse default_admin_response(const Config& config) {
    const auto local_credentials = fetch_local_credentials(config);
    const auto env_email = first_env({"ADMIN_EMAIL", "ATTENDANCE_ADMIN_EMAIL"});
    std::string username;
    std::string email;
    if (local_credentials.has_value()) {
        username = local_credentials->username;
        email = local_credentials->email.value_or("");
    } else {
        username = first_env({"ADMIN_USERNAME", "ATTENDANCE_ADMIN_USERNAME"}).value_or("");
        email = env_email.value_or("");
    }

    const AuthStatus status = get_auth_status(config);
    std::ostringstream body;
    body << "{"
         << "\"username\":" << json_string(username) << ","
         << "\"email\":" << json_string(email) << ","
         << "\"configured\":" << json_string(status.configured ? "true" : "false") << ","
         << "\"email_recovery_enabled\":" << json_string(status.email_recovery_enabled ? "true" : "false")
         << "}";
    return make_json_response(200, body.str());
}

HttpResponse workers_response(const Config& config) {
    SqliteConnection connection(config.scalable_db_path);
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            "SELECT id, employee_code, name, created_at FROM workers ORDER BY name ASC",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }

    std::ostringstream body;
    body << "[";
    bool first_row = true;
    while (sqlite3_step(statement) == SQLITE_ROW) {
        if (!first_row) {
            body << ",";
        }
        first_row = false;
        body << "{"
             << "\"id\":" << sqlite3_column_int(statement, 0) << ","
             << "\"employee_code\":" << json_string(sqlite_column_text(statement, 1)) << ","
             << "\"name\":" << json_string(sqlite_column_text(statement, 2)) << ","
             << "\"created_at\":" << json_string(sqlite_column_text(statement, 3))
             << "}";
    }
    sqlite3_finalize(statement);
    body << "]";
    return make_json_response(200, body.str());
}

HttpResponse attendance_response(const Config& config, const HttpRequest& request) {
    int limit = kDefaultAttendanceLimit;
    const auto query_values = parse_query_string(request.query);
    const auto limit_iter = query_values.find("limit");
    if (limit_iter != query_values.end()) {
        try {
            limit = std::stoi(limit_iter->second);
        } catch (const std::exception&) {
            limit = kDefaultAttendanceLimit;
        }
    }
    if (limit < 1) {
        limit = 1;
    }
    if (limit > kMaxAttendanceLimit) {
        limit = kMaxAttendanceLimit;
    }

    SqliteConnection connection(config.scalable_db_path);
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            R"sql(
                SELECT attendance_events.id, attendance_events.worker_id, workers.employee_code, workers.name,
                       attendance_events.camera_id, attendance_events.matched_score,
                       attendance_events.raw_sensor_value,
                       attendance_events.alcohol_ppb, attendance_events.cannabis_ppb,
                       attendance_events.alcohol_clear, attendance_events.cannabis_clear,
                       attendance_events.attendance_marked, attendance_events.created_at
                FROM attendance_events
                JOIN workers ON workers.id = attendance_events.worker_id
                ORDER BY attendance_events.created_at DESC
                LIMIT ?
            )sql",
            -1,
            &statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }

    sqlite3_bind_int(statement, 1, limit);
    std::ostringstream body;
    body << "[";
    bool first_row = true;
    while (sqlite3_step(statement) == SQLITE_ROW) {
        if (!first_row) {
            body << ",";
        }
        first_row = false;
        body << "{"
             << "\"id\":" << sqlite3_column_int(statement, 0) << ","
             << "\"worker_id\":" << sqlite3_column_int(statement, 1) << ","
             << "\"employee_code\":" << json_string(sqlite_column_text(statement, 2)) << ","
             << "\"name\":" << json_string(sqlite_column_text(statement, 3)) << ","
             << "\"camera_id\":" << json_string(sqlite_column_text(statement, 4)) << ","
             << "\"matched_score\":" << sqlite3_column_double(statement, 5) << ",";

        if (sqlite3_column_type(statement, 6) == SQLITE_NULL) {
            body << "\"raw_sensor_value\":null,";
        } else {
            body << "\"raw_sensor_value\":" << sqlite3_column_double(statement, 6) << ",";
        }

        body << "\"alcohol_ppb\":" << sqlite3_column_double(statement, 7) << ","
             << "\"cannabis_ppb\":" << sqlite3_column_double(statement, 8) << ","
             << "\"alcohol_clear\":" << bool_json(sqlite3_column_int(statement, 9) != 0) << ","
             << "\"cannabis_clear\":" << bool_json(sqlite3_column_int(statement, 10) != 0) << ","
             << "\"attendance_marked\":" << bool_json(sqlite3_column_int(statement, 11) != 0) << ","
             << "\"created_at\":" << json_string(sqlite_column_text(statement, 12))
             << "}";
    }
    sqlite3_finalize(statement);
    body << "]";
    return make_json_response(200, body.str());
}

HttpResponse delete_worker_response(const Config& config, const std::string& employee_code) {
    SqliteConnection connection(config.scalable_db_path);
    exec_sql(connection.get(), "BEGIN IMMEDIATE");

    sqlite3_stmt* fetch_statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            "SELECT id, employee_code, name FROM workers WHERE employee_code = ?",
            -1,
            &fetch_statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }
    sqlite3_bind_text(fetch_statement, 1, employee_code.c_str(), -1, SQLITE_TRANSIENT);
    if (sqlite3_step(fetch_statement) != SQLITE_ROW) {
        sqlite3_finalize(fetch_statement);
        exec_sql(connection.get(), "ROLLBACK");
        return error_response(400, "No worker found for employee code '" + employee_code + "'.");
    }

    const int worker_id = sqlite3_column_int(fetch_statement, 0);
    const std::string worker_code = sqlite_column_text(fetch_statement, 1);
    const std::string worker_name = sqlite_column_text(fetch_statement, 2);
    sqlite3_finalize(fetch_statement);

    sqlite3_stmt* delete_statement = nullptr;
    const std::array<std::string, 3> delete_queries = {
        "DELETE FROM attendance_events WHERE worker_id = ?",
        "DELETE FROM worker_embeddings WHERE worker_id = ?",
        "DELETE FROM workers WHERE id = ?",
    };

    for (const std::string& query : delete_queries) {
        if (sqlite3_prepare_v2(connection.get(), query.c_str(), -1, &delete_statement, nullptr) != SQLITE_OK) {
            throw std::runtime_error(sqlite3_errmsg(connection.get()));
        }
        sqlite3_bind_int(delete_statement, 1, worker_id);
        if (sqlite3_step(delete_statement) != SQLITE_DONE) {
            std::string message = sqlite3_errmsg(connection.get());
            sqlite3_finalize(delete_statement);
            throw std::runtime_error(message);
        }
        sqlite3_finalize(delete_statement);
    }

    exec_sql(connection.get(), "COMMIT");
    const int index_size = count_rows(connection.get(), "SELECT COUNT(*) FROM worker_embeddings");
    std::ostringstream body;
    body << "{"
         << "\"worker_id\":" << worker_id << ","
         << "\"employee_code\":" << json_string(worker_code) << ","
         << "\"name\":" << json_string(worker_name) << ","
         << "\"deleted\":true,"
         << "\"index_size\":" << index_size
         << "}";
    return make_json_response(200, body.str());
}

HttpResponse delete_attendance_response(const Config& config, int attendance_id) {
    SqliteConnection connection(config.scalable_db_path);
    exec_sql(connection.get(), "BEGIN IMMEDIATE");

    sqlite3_stmt* fetch_statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            R"sql(
                SELECT attendance_events.id, attendance_events.worker_id, workers.employee_code, workers.name
                FROM attendance_events
                JOIN workers ON workers.id = attendance_events.worker_id
                WHERE attendance_events.id = ?
            )sql",
            -1,
            &fetch_statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }
    sqlite3_bind_int(fetch_statement, 1, attendance_id);
    if (sqlite3_step(fetch_statement) != SQLITE_ROW) {
        sqlite3_finalize(fetch_statement);
        exec_sql(connection.get(), "ROLLBACK");
        return error_response(400, "No attendance record found for id '" + std::to_string(attendance_id) + "'.");
    }

    const int found_id = sqlite3_column_int(fetch_statement, 0);
    const int worker_id = sqlite3_column_int(fetch_statement, 1);
    const std::string employee_code = sqlite_column_text(fetch_statement, 2);
    const std::string name = sqlite_column_text(fetch_statement, 3);
    sqlite3_finalize(fetch_statement);

    sqlite3_stmt* delete_statement = nullptr;
    if (sqlite3_prepare_v2(
            connection.get(),
            "DELETE FROM attendance_events WHERE id = ?",
            -1,
            &delete_statement,
            nullptr
        ) != SQLITE_OK) {
        throw std::runtime_error(sqlite3_errmsg(connection.get()));
    }
    sqlite3_bind_int(delete_statement, 1, attendance_id);
    if (sqlite3_step(delete_statement) != SQLITE_DONE) {
        std::string message = sqlite3_errmsg(connection.get());
        sqlite3_finalize(delete_statement);
        throw std::runtime_error(message);
    }
    sqlite3_finalize(delete_statement);
    exec_sql(connection.get(), "COMMIT");

    std::ostringstream body;
    body << "{"
         << "\"id\":" << found_id << ","
         << "\"worker_id\":" << worker_id << ","
         << "\"employee_code\":" << json_string(employee_code) << ","
         << "\"name\":" << json_string(name) << ","
         << "\"deleted\":true"
         << "}";
    return make_json_response(200, body.str());
}

std::string http_status_text(int status) {
    switch (status) {
        case 200:
            return "OK";
        case 204:
            return "No Content";
        case 400:
            return "Bad Request";
        case 401:
            return "Unauthorized";
        case 404:
            return "Not Found";
        case 405:
            return "Method Not Allowed";
        case 500:
            return "Internal Server Error";
        case 501:
            return "Not Implemented";
        case 503:
            return "Service Unavailable";
        default:
            return "OK";
    }
}

HttpResponse options_response() {
    HttpResponse response;
    response.status = 204;
    response.content_type = "text/plain";
    response.body.clear();
    return response;
}

HttpResponse route_request(const Config& config, const HttpRequest& request) {
    if (request.method == "OPTIONS") {
        return options_response();
    }

    if (request.path == "/health" && request.method == "GET") {
        return make_json_response(200, "{\"status\":\"ok\",\"backend\":\"native-cpp\"}");
    }

    if (request.path == "/api/v2/auth/status" && request.method == "GET") {
        return auth_status_response(config);
    }

    if (request.path == "/api/v2/auth/login" && request.method == "POST") {
        return login_response(config, request);
    }

    if (request.path == "/api/v2/auth/setup" && request.method == "POST") {
        return setup_response(config, request);
    }

    if (request.path == "/api/v2/auth/reset" && request.method == "POST") {
        return reset_response(config, request);
    }

    if (request.path == "/api/v2/auth/recovery/username/request" && request.method == "POST") {
        return recovery_request_response(config, request, "username");
    }

    if (request.path == "/api/v2/auth/recovery/username/verify" && request.method == "POST") {
        return verify_username_recovery_response(config, request);
    }

    if (request.path == "/api/v2/auth/recovery/password/request" && request.method == "POST") {
        return recovery_request_response(config, request, "password");
    }

    if (request.path == "/api/v2/auth/recovery/password/verify" && request.method == "POST") {
        return verify_password_recovery_response(config, request);
    }

    if (request.path == "/api/v2/auth/logout" && request.method == "POST") {
        return logout_response(config, request);
    }

    const auto session = require_auth(config, request);
    const bool needs_auth = request.path.rfind("/api/v2/", 0) == 0 && request.path != "/api/v2/auth/status" && request.path != "/api/v2/auth/login";
    if (needs_auth && !session.has_value()) {
        return error_response(401, "Authentication required.");
    }

    if (request.path == "/api/v2/auth/me" && request.method == "GET") {
        return me_response(*session);
    }

    if (request.path == "/api/v2/auth/default-admin" && request.method == "GET") {
        return default_admin_response(config);
    }

    if (request.path == "/api/v2/status" && request.method == "GET") {
        return service_status_response(config);
    }

    if (request.path == "/api/v2/architecture" && request.method == "GET") {
        return architecture_response(config);
    }

    if (request.path == "/api/v2/workers" && request.method == "GET") {
        return workers_response(config);
    }

    if (request.path == "/api/v2/workers/enroll" && request.method == "POST") {
        return enroll_worker_response(config, request);
    }

    if (request.path.rfind("/api/v2/workers/", 0) == 0 && request.method == "DELETE") {
        const std::string employee_code = url_decode(request.path.substr(std::string("/api/v2/workers/").size()));
        return delete_worker_response(config, employee_code);
    }

    if (request.path == "/api/v2/attendance" && request.method == "GET") {
        return attendance_response(config, request);
    }

    if (request.path.rfind("/api/v2/attendance/", 0) == 0 && request.method == "DELETE") {
        const std::string suffix = request.path.substr(std::string("/api/v2/attendance/").size());
        try {
            return delete_attendance_response(config, std::stoi(suffix));
        } catch (const std::exception&) {
            return error_response(400, "Attendance id must be a number.");
        }
    }

    if (request.path == "/api/v2/recognitions" && request.method == "POST") {
        return recognize_response(config, request);
    }

    if (request.path == "/api/v2/detections" && request.method == "POST") {
        return detect_response(request);
    }

    if (request.path == "/api/v2/breath-tests" && request.method == "POST") {
        return run_breath_test_response(config, request);
    }

    if (request.path == "/api/v2/breath-tests/start" && request.method == "POST") {
        return start_breath_test_response(config, request);
    }

    if (request.path == "/api/v2/breath-tests/complete" && request.method == "POST") {
        return complete_breath_test_response(config, request);
    }

    if (request.path.rfind("/api/v2/breath-tests/", 0) == 0 && request.method == "DELETE") {
        const std::string session_id = url_decode(request.path.substr(std::string("/api/v2/breath-tests/").size()));
        return cancel_breath_test_response(session_id);
    }

    if (request.path == "/api/v2/local-camera/status" && request.method == "GET") {
        return local_camera_status_response();
    }

    if (request.path == "/api/v2/local-camera/start" && request.method == "POST") {
        return start_local_camera_response();
    }

    if (request.path == "/api/v2/local-camera/stop" && request.method == "POST") {
        return stop_local_camera_response();
    }

    if (request.path == "/api/v2/local-camera/frame" && request.method == "GET") {
        return local_camera_frame_response();
    }

    if (request.path == "/api/v2/local-camera/stream.mjpg" && request.method == "GET") {
        return error_response(500, "MJPEG streaming should be handled directly by the native socket server.");
    }

    if (request.path == "/api/v2/index/rebuild" && request.method == "POST") {
        return rebuild_index_response(config);
    }

    if (request.path.rfind("/api/", 0) == 0) {
        return error_response(404, "Not Found");
    }

    return make_json_response(
        200,
        "{\"status\":\"native-cpp-backend\",\"message\":\"The Linux desktop shell is using the native migration backend.\"}"
    );
}

std::optional<HttpRequest> parse_request(const std::string& raw_request) {
    const std::size_t header_end = raw_request.find("\r\n\r\n");
    if (header_end == std::string::npos) {
        return std::nullopt;
    }

    HttpRequest request;
    std::istringstream stream(raw_request.substr(0, header_end));
    std::string request_line;
    if (!std::getline(stream, request_line)) {
        return std::nullopt;
    }
    if (!request_line.empty() && request_line.back() == '\r') {
        request_line.pop_back();
    }

    std::istringstream request_line_stream(request_line);
    std::string target;
    std::string version;
    if (!(request_line_stream >> request.method >> target >> version)) {
        return std::nullopt;
    }

    request.target = target;
    const std::size_t query_separator = target.find('?');
    request.path = query_separator == std::string::npos ? target : target.substr(0, query_separator);
    request.query = query_separator == std::string::npos ? "" : target.substr(query_separator + 1);

    std::string header_line;
    while (std::getline(stream, header_line)) {
        if (!header_line.empty() && header_line.back() == '\r') {
            header_line.pop_back();
        }
        if (header_line.empty()) {
            continue;
        }
        const std::size_t colon = header_line.find(':');
        if (colon == std::string::npos) {
            continue;
        }
        const std::string key = to_lower_copy(trim(header_line.substr(0, colon)));
        const std::string value = trim(header_line.substr(colon + 1));
        request.headers[key] = value;
    }

    request.body = raw_request.substr(header_end + 4);
    return request;
}

void send_response(int socket_fd, const HttpResponse& response) {
    std::ostringstream stream;
    stream << "HTTP/1.1 " << response.status << ' ' << http_status_text(response.status) << "\r\n";
    stream << "Content-Type: " << response.content_type << "\r\n";
    stream << "Content-Length: " << response.body.size() << "\r\n";
    stream << "Connection: close\r\n";
    stream << "Cache-Control: no-store\r\n";
    stream << "Access-Control-Allow-Origin: *\r\n";
    stream << "Access-Control-Allow-Headers: Authorization, Content-Type, X-Auth-Token\r\n";
    stream << "Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS\r\n";
    for (const auto& [key, value] : response.headers) {
        stream << key << ": " << value << "\r\n";
    }
    stream << "\r\n";
    stream << response.body;

    const std::string payload = stream.str();
    std::size_t sent = 0;
    while (sent < payload.size()) {
#if defined(__linux__) || defined(__APPLE__)
        const ssize_t written = send(socket_fd, payload.data() + sent, payload.size() - sent, 0);
#else
        const int written = 0;
#endif
        if (written <= 0) {
            break;
        }
        sent += static_cast<std::size_t>(written);
    }
}

bool send_all_bytes(int socket_fd, const char* data, std::size_t size) {
    std::size_t sent = 0;
    while (sent < size) {
#if defined(__linux__) || defined(__APPLE__)
        const ssize_t written = send(socket_fd, data + sent, size - sent, 0);
#else
        const int written = 0;
#endif
        if (written <= 0) {
            return false;
        }
        sent += static_cast<std::size_t>(written);
    }
    return true;
}

bool send_all_bytes(int socket_fd, const std::string& data) {
    return send_all_bytes(socket_fd, data.data(), data.size());
}

void send_camera_stream(int client_socket, const Config& config, const HttpRequest& request) {
    const auto session = require_auth(config, request);
    if (!session.has_value()) {
        send_response(client_socket, error_response(401, "Authentication required."));
        return;
    }

    try {
        if (!camera_runtime().is_running()) {
            camera_runtime().start();
        }
    } catch (const std::exception& exc) {
        send_response(client_socket, error_response(503, exc.what()));
        return;
    }

    std::ostringstream header_stream;
    header_stream
        << "HTTP/1.1 200 OK\r\n"
        << "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
        << "Connection: close\r\n"
        << "Cache-Control: no-store\r\n"
        << "Access-Control-Allow-Origin: *\r\n"
        << "Access-Control-Allow-Headers: Authorization, Content-Type, X-Auth-Token\r\n"
        << "Access-Control-Allow-Methods: GET, POST, DELETE, OPTIONS\r\n\r\n";
    if (!send_all_bytes(client_socket, header_stream.str())) {
        return;
    }

    while (true) {
        try {
            const std::vector<unsigned char> frame_bytes = camera_runtime().get_frame_bytes();
            std::ostringstream part_header;
            part_header
                << "--frame\r\n"
                << "Content-Type: image/jpeg\r\n"
                << "Cache-Control: no-store, no-cache, must-revalidate\r\n"
                << "Content-Length: " << frame_bytes.size() << "\r\n\r\n";

            if (!send_all_bytes(client_socket, part_header.str())) {
                break;
            }
            if (!send_all_bytes(
                    client_socket,
                    reinterpret_cast<const char*>(frame_bytes.data()),
                    frame_bytes.size()
                )) {
                break;
            }
            if (!send_all_bytes(client_socket, "\r\n", 2)) {
                break;
            }
        } catch (const std::exception&) {
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(30));
    }
}

std::optional<std::string> read_http_request(int socket_fd) {
    std::string buffer;
    buffer.reserve(kRequestChunkSize);
    int content_length = 0;
    bool headers_complete = false;

    while (static_cast<int>(buffer.size()) < kMaxRequestBytes) {
        std::array<char, kRequestChunkSize> chunk {};
#if defined(__linux__) || defined(__APPLE__)
        const ssize_t bytes_read = recv(socket_fd, chunk.data(), chunk.size(), 0);
#else
        const int bytes_read = -1;
#endif
        if (bytes_read <= 0) {
            break;
        }
        buffer.append(chunk.data(), static_cast<std::size_t>(bytes_read));

        if (!headers_complete) {
            const std::size_t header_end = buffer.find("\r\n\r\n");
            if (header_end != std::string::npos) {
                headers_complete = true;
                const std::string headers = buffer.substr(0, header_end);
                const std::regex content_length_pattern("content-length\\s*:\\s*(\\d+)", std::regex::icase);
                std::smatch match;
                if (std::regex_search(headers, match, content_length_pattern)) {
                    content_length = std::stoi(match[1].str());
                }
                const std::size_t expected_size = header_end + 4 + static_cast<std::size_t>(content_length);
                if (buffer.size() >= expected_size) {
                    return buffer.substr(0, expected_size);
                }
            }
        } else {
            const std::size_t header_end = buffer.find("\r\n\r\n");
            const std::size_t expected_size = header_end + 4 + static_cast<std::size_t>(content_length);
            if (buffer.size() >= expected_size) {
                return buffer.substr(0, expected_size);
            }
        }
    }

    if (!buffer.empty()) {
        return buffer;
    }
    return std::nullopt;
}

void handle_client(int client_socket, const Config& config) {
    try {
        const auto raw_request = read_http_request(client_socket);
        if (!raw_request.has_value()) {
            send_response(client_socket, error_response(400, "Could not read request."));
        } else {
            const auto request = parse_request(*raw_request);
            if (!request.has_value()) {
                send_response(client_socket, error_response(400, "Could not parse request."));
            } else if (request->path == "/api/v2/local-camera/stream.mjpg" && request->method == "GET") {
                send_camera_stream(client_socket, config, *request);
            } else {
                send_response(client_socket, route_request(config, *request));
            }
        }
    } catch (const std::exception& exc) {
        send_response(client_socket, error_response(500, exc.what()));
    }

#if defined(__linux__) || defined(__APPLE__)
    close(client_socket);
#endif
}

void run_server(const Config& config) {
#if !defined(__linux__) && !defined(__APPLE__)
    throw std::runtime_error("The native C++ backend currently targets Linux desktop builds.");
#else
    const int server_socket = socket(AF_INET, SOCK_STREAM, 0);
    if (server_socket < 0) {
        throw std::runtime_error("Could not create server socket.");
    }

    int reuse = 1;
    setsockopt(server_socket, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    sockaddr_in address {};
    address.sin_family = AF_INET;
    address.sin_port = htons(static_cast<uint16_t>(config.port));
    if (config.host == "0.0.0.0") {
        address.sin_addr.s_addr = htonl(INADDR_ANY);
    } else if (inet_pton(AF_INET, config.host.c_str(), &address.sin_addr) != 1) {
        close(server_socket);
        throw std::runtime_error("ATTENDANCE_WEB_HOST must be a valid IPv4 address for the native backend.");
    }

    if (bind(server_socket, reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0) {
        const std::string message = std::string("Could not bind native backend socket: ") + std::strerror(errno);
        close(server_socket);
        throw std::runtime_error(message);
    }

    if (listen(server_socket, kListenBacklog) < 0) {
        const std::string message = std::string("Could not listen on native backend socket: ") + std::strerror(errno);
        close(server_socket);
        throw std::runtime_error(message);
    }

    std::cout << "Native C++ backend listening on http://" << config.host << ':' << config.port << '/' << std::endl;

    while (true) {
        sockaddr_in client_address {};
        socklen_t client_length = sizeof(client_address);
        const int client_socket = accept(server_socket, reinterpret_cast<sockaddr*>(&client_address), &client_length);
        if (client_socket < 0) {
            continue;
        }
        std::thread(handle_client, client_socket, config).detach();
    }
#endif
}

}  // namespace

int main() {
    try {
        const Config config = load_config();
        initialize_session_store(config);
        initialize_scalable_store(config);
        run_server(config);
        return 0;
    } catch (const std::exception& exc) {
        std::cerr << "Native C++ backend failed: " << exc.what() << std::endl;
        return 1;
    }
}
