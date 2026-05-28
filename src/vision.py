from pathlib import Path

import cv2

from src.config import FACE_SIZE


FACE_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
EYE_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_eye_tree_eyeglasses.xml"


def get_face_cascade() -> cv2.CascadeClassifier:
    cascade = cv2.CascadeClassifier(FACE_CASCADE_PATH)
    if cascade.empty():
        raise RuntimeError(f"Could not load Haar cascade from {FACE_CASCADE_PATH}")
    return cascade


def get_eye_cascade() -> cv2.CascadeClassifier:
    cascade = cv2.CascadeClassifier(EYE_CASCADE_PATH)
    if cascade.empty():
        raise RuntimeError(f"Could not load eye cascade from {EYE_CASCADE_PATH}")
    return cascade


def detect_faces(gray_frame, scale_factor: float = 1.2, min_neighbors: int = 5):
    cascade = get_face_cascade()
    return cascade.detectMultiScale(gray_frame, scaleFactor=scale_factor, minNeighbors=min_neighbors, minSize=(80, 80))


def detect_eyes(face_roi, scale_factor: float = 1.1, min_neighbors: int = 4):
    cascade = get_eye_cascade()
    return cascade.detectMultiScale(face_roi, scaleFactor=scale_factor, minNeighbors=min_neighbors, minSize=(20, 20))


def extract_face_region(gray_frame, face_rect) -> object:
    x, y, w, h = face_rect
    face = gray_frame[y : y + h, x : x + w]
    return cv2.resize(face, FACE_SIZE)
