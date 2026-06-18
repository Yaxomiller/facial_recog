cmake_minimum_required(VERSION 3.16)

project(attendance-native-backend LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(SQLite3 REQUIRED)
find_package(Threads REQUIRED)
find_package(OpenCV REQUIRED COMPONENTS core imgcodecs imgproc objdetect videoio)

add_executable(attendance-native-backend
    src/breath_analyzer.cpp
    src/camera_runtime.cpp
    src/detector.cpp
    src/main.cpp
    src/recognizer.cpp
)

target_link_libraries(attendance-native-backend
    PRIVATE
        ${OpenCV_LIBS}
        SQLite::SQLite3
        Threads::Threads
)

if(UNIX AND NOT APPLE)
    find_library(CRYPT_LIBRARY crypt)
    if(CRYPT_LIBRARY)
        target_link_libraries(attendance-native-backend PRIVATE ${CRYPT_LIBRARY})
    endif()
endif()

install(TARGETS attendance-native-backend RUNTIME DESTINATION .)
