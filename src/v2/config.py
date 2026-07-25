import os
from pathlib import Path

from src.runtime_paths import DATA_DIR


SCALABLE_DB_FILE = Path(os.getenv("ATTENDANCE_DB_FILE", str(DATA_DIR / "scalable_attendance.db")))
VECTOR_INDEX_FILE = Path(os.getenv("ATTENDANCE_VECTOR_INDEX_FILE", str(DATA_DIR / "vector_index_v2.npz")))
EMBEDDING_SIZE = 256
FACE_SIZE = (112, 112)
MATCH_THRESHOLD = float(os.getenv("ATTENDANCE_MATCH_THRESHOLD", "0.68"))
AMBIGUITY_MARGIN = 0.04
ATTENDANCE_COOLDOWN_HOURS = 12
MAX_TOP_K = 5
MAX_FACES_PER_REQUEST = int(os.getenv("ATTENDANCE_MAX_FACES_PER_REQUEST", "10"))
RECOGNITION_CACHE_TTL_SECONDS = int(os.getenv("ATTENDANCE_RECOGNITION_CACHE_TTL_SECONDS", "5"))
DEFAULT_LIST_LIMIT = int(os.getenv("ATTENDANCE_DEFAULT_LIST_LIMIT", "100"))
EMBEDDING_BACKEND = os.getenv("ATTENDANCE_EMBEDDING_BACKEND", "lbph")
VECTOR_INDEX_BACKEND = os.getenv("ATTENDANCE_VECTOR_INDEX_BACKEND", "numpy")
BREATH_ANALYZER_MODE = os.getenv("ATTENDANCE_BREATH_ANALYZER_MODE", "mock").strip().lower()

# --- Breath electronics (DiDies STM32 sensor board over SPI) -----------------
# Ported from the BreathCheck handheld analyzer so both products drive the same
# hardware identically. Readings are trapezoidal INTEGRALS of the delta above
# the fresh-air baseline, in mV*s (the AL-05P datasheet specs linearity as the
# integral of output). The *_PPB field names are kept for schema/DB
# compatibility, but the values they carry are mV*s integrals.
BREATH_ALCOHOL_THRESHOLD_PPB = float(os.getenv("ATTENDANCE_BREATH_ALCOHOL_THRESHOLD_PPB", "15"))
BREATH_CANNABIS_THRESHOLD_PPB = float(os.getenv("ATTENDANCE_BREATH_CANNABIS_THRESHOLD_PPB", "3"))

# Measurement cycle. The blow window is the operator-visible sample time;
# purge/baseline are hardware timings that run before it.
BREATH_SAMPLE_SECONDS = max(0.0, float(os.getenv("ATTENDANCE_BREATH_SAMPLE_SECONDS", "10")))
BREATH_PURGE_SECONDS = max(0.0, float(os.getenv("ATTENDANCE_BREATH_PURGE_SECONDS", "15")))
BREATH_BASELINE_SECONDS = max(0.0, float(os.getenv("ATTENDANCE_BREATH_BASELINE_SECONDS", "5")))

# STM32 SPI bridge wiring (Radxa Cubie GPIO numbering).
BREATH_SPI_DEVICE = os.getenv("ATTENDANCE_BREATH_SPI_DEVICE", "/dev/spidev1.0").strip() or "/dev/spidev1.0"
BREATH_SPI_MODE = int(os.getenv("ATTENDANCE_BREATH_SPI_MODE", "0"))
BREATH_SPI_SPEED_HZ = int(os.getenv("ATTENDANCE_BREATH_SPI_SPEED_HZ", "500000"))
BREATH_GPIO_CHIP = os.getenv("ATTENDANCE_BREATH_GPIO_CHIP", "/dev/gpiochip1").strip() or "/dev/gpiochip1"
BREATH_BOARD_ENABLE_GPIO = int(os.getenv("ATTENDANCE_BREATH_BOARD_ENABLE_GPIO", "256"))  # BRD_ON, PI0 pin 26
BREATH_READY_GPIO = int(os.getenv("ATTENDANCE_BREATH_READY_GPIO", "257"))                # doorbell, PI1 pin 32
BREATH_PUMP_GPIO = int(os.getenv("ATTENDANCE_BREATH_PUMP_GPIO", "271"))                  # pump, PI15, ACTIVE HIGH
BREATH_DOORBELL_TIMEOUT_SECONDS = float(os.getenv("ATTENDANCE_BREATH_DOORBELL_TIMEOUT_SECONDS", "5.0"))
BREATH_BOARD_RESET_SECONDS = float(os.getenv("ATTENDANCE_BREATH_BOARD_RESET_SECONDS", "0.1"))
BREATH_BOARD_BOOT_SECONDS = float(os.getenv("ATTENDANCE_BREATH_BOARD_BOOT_SECONDS", "1.0"))
BREATH_STREAM_DEAD_SECONDS = float(os.getenv("ATTENDANCE_BREATH_STREAM_DEAD_SECONDS", "10.0"))

# Unit conversion — keep in sync with the STM32 firmware.
BREATH_RTIA_KOHM = float(os.getenv("ATTENDANCE_BREATH_RTIA_KOHM", "4.0"))  # AD5941 LPTIARTIA_4K

# Cannabis conformity score (upper/lower area ratio). The exhale trace (PID
# delta above the fresh-air baseline, in mV) is a positive bell; a horizontal
# line at this threshold splits the area under it into an upper section (the
# part poking above the line) and a lower section (the part beneath it). The
# reported conformity score is upper / lower.
BREATH_CANNABIS_THRESHOLD_MV = float(os.getenv("ATTENDANCE_BREATH_CANNABIS_THRESHOLD_MV", "0.4"))

# Alcohol-cell stabilization at app start (fresh-air settle).
BREATH_SETTLE_SLOPE_NA_S = float(os.getenv("ATTENDANCE_BREATH_SETTLE_SLOPE_NA_S", "30.0"))
BREATH_SETTLE_WINDOW_MS = float(os.getenv("ATTENDANCE_BREATH_SETTLE_WINDOW_MS", "10000.0"))
BREATH_STABILIZE_MAX_S = float(os.getenv("ATTENDANCE_BREATH_STABILIZE_MAX_S", "180.0"))

# Mock reading ranges — integrals in mV*s, matching the live measurement.
BREATH_MOCK_ALCOHOL_MIN = float(os.getenv("ATTENDANCE_BREATH_MOCK_ALCOHOL_MIN", "0"))
BREATH_MOCK_ALCOHOL_MAX = float(os.getenv("ATTENDANCE_BREATH_MOCK_ALCOHOL_MAX", "30"))
BREATH_MOCK_CANNABIS_MIN = float(os.getenv("ATTENDANCE_BREATH_MOCK_CANNABIS_MIN", "0"))
BREATH_MOCK_CANNABIS_MAX = float(os.getenv("ATTENDANCE_BREATH_MOCK_CANNABIS_MAX", "6"))
ALLOW_BACKEND_FALLBACK = os.getenv("ATTENDANCE_ALLOW_BACKEND_FALLBACK", "true").lower() in {"1", "true", "yes", "on"}
LBPH_CONFIDENCE_THRESHOLD = float(os.getenv("ATTENDANCE_LBPH_CONFIDENCE_THRESHOLD", "78"))
OPEN_SET_MIN_SCORE = float(os.getenv("ATTENDANCE_OPEN_SET_MIN_SCORE", "0.72"))
OPEN_SET_SUPPORT_SCORE = float(os.getenv("ATTENDANCE_OPEN_SET_SUPPORT_SCORE", "0.66"))
OPEN_SET_MIN_CENTROID_SCORE = min(0.70, float(os.getenv("ATTENDANCE_OPEN_SET_MIN_CENTROID_SCORE", "0.70")))
MAX_PROFILE_CENTROID_THRESHOLD = min(
    0.70,
    float(os.getenv("ATTENDANCE_MAX_PROFILE_CENTROID_THRESHOLD", "0.70")),
)
OPEN_SET_CENTROID_MARGIN = float(os.getenv("ATTENDANCE_OPEN_SET_CENTROID_MARGIN", "0.08"))
MIN_ENROLLMENT_IMAGES = int(os.getenv("ATTENDANCE_MIN_ENROLLMENT_IMAGES", "3"))
DESCRIPTOR_VARIANT_REQUIRED_HITS = int(os.getenv("ATTENDANCE_DESCRIPTOR_VARIANT_REQUIRED_HITS", "2"))
SINGLE_PROFILE_MIN_SCORE = float(os.getenv("ATTENDANCE_SINGLE_PROFILE_MIN_SCORE", "0.84"))
SINGLE_PROFILE_SUPPORT_SCORE = float(os.getenv("ATTENDANCE_SINGLE_PROFILE_SUPPORT_SCORE", "0.72"))
SINGLE_PROFILE_MIN_CENTROID_SCORE = min(
    0.70,
    float(os.getenv("ATTENDANCE_SINGLE_PROFILE_MIN_CENTROID_SCORE", "0.70")),
)
SINGLE_PROFILE_REQUIRED_SUPPORT_HITS = int(os.getenv("ATTENDANCE_SINGLE_PROFILE_REQUIRED_SUPPORT_HITS", "3"))
SINGLE_PROFILE_LBPH_CONFIDENCE_THRESHOLD = float(
    os.getenv("ATTENDANCE_SINGLE_PROFILE_LBPH_CONFIDENCE_THRESHOLD", "55")
)
MIN_FACE_WIDTH = int(os.getenv("ATTENDANCE_MIN_FACE_WIDTH", "90"))
MIN_FACE_HEIGHT = int(os.getenv("ATTENDANCE_MIN_FACE_HEIGHT", "90"))
MIN_FACE_BLUR_VARIANCE = float(os.getenv("ATTENDANCE_MIN_FACE_BLUR_VARIANCE", "30"))
MIN_FACE_BRIGHTNESS = float(os.getenv("ATTENDANCE_MIN_FACE_BRIGHTNESS", "35"))
MAX_FACE_BRIGHTNESS = float(os.getenv("ATTENDANCE_MAX_FACE_BRIGHTNESS", "220"))
