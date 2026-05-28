from __future__ import annotations

import argparse
import importlib
import json
import os
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


@dataclass
class DatasetBenchmarkReport:
    dataset_root: str
    people_enrolled: int
    enroll_images: int
    test_images: int
    detected_faces: int
    detection_rate: float
    correct_matches: int
    false_rejects: int
    misidentifications: int
    accuracy: float
    avg_detect_ms: float
    avg_recognize_ms: float
    p95_recognize_ms: float
    active_detector: str
    active_embedder: str
    active_index: str
    output_file: str
    sample_failures: list[dict[str, str]]


@dataclass
class LoadBenchmarkReport:
    workers_indexed: int
    probes: int
    vector_dimension: int
    avg_search_ms: float
    p95_search_ms: float
    max_search_ms: float
    active_embedder: str
    active_index: str
    output_file: str


def run_dataset_benchmark(
    dataset_root: Path,
    enroll_per_person: int,
    max_people: int,
    min_images_per_person: int,
    top_k: int,
) -> DatasetBenchmarkReport:
    benchmark_dir = _prepare_benchmark_environment("dataset")
    service = _build_service()
    people = _collect_people(dataset_root=dataset_root, max_people=max_people, min_images_per_person=min_images_per_person)
    if not people:
        raise RuntimeError("No valid people found in dataset root. Expected one folder per person with image files.")

    enroll_images = 0
    test_set: list[tuple[str, str, Path]] = []
    skipped_people = 0
    for index, (person_name, images) in enumerate(people, start=1):
        employee_code = f"BENCH_{index:04d}"
        enroll_paths, test_paths = _split_detectable_images(service, images, enroll_per_person=enroll_per_person)
        if not test_paths:
            skipped_people += 1
            continue

        service.enroll_worker(
            employee_code=employee_code,
            name=person_name,
            image_bytes_list=[path.read_bytes() for path in enroll_paths],
            replace_existing=True,
        )
        enroll_images += len(enroll_paths)
        for test_path in test_paths:
            test_set.append((employee_code, person_name, test_path))

    if not test_set:
        raise RuntimeError(
            "No benchmarkable test images were found. Try generating a milder synthetic dataset or reduce --enroll-per-person."
        )

    detected_faces = 0
    correct_matches = 0
    false_rejects = 0
    misidentifications = 0
    detect_latencies: list[float] = []
    recognize_latencies: list[float] = []
    sample_failures: list[dict[str, str]] = []

    for sample_index, (employee_code, person_name, image_path) in enumerate(test_set, start=1):
        image_bytes = image_path.read_bytes()

        detect_started = time.perf_counter()
        detection = service.detect(image_bytes)
        detect_latencies.append((time.perf_counter() - detect_started) * 1000.0)
        if detection.detected_faces > 0:
            detected_faces += 1

        recognize_started = time.perf_counter()
        result = service.recognize(
            image_bytes=image_bytes,
            camera_id=f"benchmark-{sample_index}",
            top_k=top_k,
        )
        recognize_latencies.append((time.perf_counter() - recognize_started) * 1000.0)

        predicted_code = result.matches[0].employee_code if result.matches else None
        if predicted_code == employee_code:
            correct_matches += 1
            continue
        if predicted_code is None:
            false_rejects += 1
            if len(sample_failures) < 12:
                reason = result.debug_faces[0].reason if result.debug_faces else "No debug reason returned."
                sample_failures.append(
                    {
                        "expected": person_name,
                        "predicted": "unknown",
                        "file": str(image_path),
                        "reason": reason,
                    }
                )
            continue

        misidentifications += 1
        if len(sample_failures) < 12:
            predicted_name = result.matches[0].name
            reason = result.debug_faces[0].reason if result.debug_faces else "Wrong identity selected."
            sample_failures.append(
                {
                    "expected": person_name,
                    "predicted": predicted_name,
                    "file": str(image_path),
                    "reason": reason,
                }
            )

    report = DatasetBenchmarkReport(
        dataset_root=str(dataset_root),
        people_enrolled=len(people),
        enroll_images=enroll_images,
        test_images=len(test_set),
        detected_faces=detected_faces,
        detection_rate=_safe_rate(detected_faces, len(test_set)),
        correct_matches=correct_matches,
        false_rejects=false_rejects,
        misidentifications=misidentifications,
        accuracy=_safe_rate(correct_matches, len(test_set)),
        avg_detect_ms=_average(detect_latencies),
        avg_recognize_ms=_average(recognize_latencies),
        p95_recognize_ms=_p95(recognize_latencies),
        active_detector=service.status().active_detector,
        active_embedder=service.status().active_embedder,
        active_index=service.status().active_index,
        output_file=str(benchmark_dir / "dataset_report.json"),
        sample_failures=sample_failures,
    )
    if skipped_people:
        report.sample_failures.append(
            {
                "expected": "benchmark",
                "predicted": "skipped",
                "file": f"{skipped_people} people",
                "reason": "Some synthetic identities did not have enough detectable images for enrollment and testing.",
            }
        )
    _write_report(Path(report.output_file), asdict(report))
    return report


def run_load_benchmark(workers: int, probes: int) -> LoadBenchmarkReport:
    benchmark_dir = _prepare_benchmark_environment("load")
    service = _build_service()
    dimension = service.embedder.vector_size or 304
    rng = np.random.default_rng(42)
    worker_ids = list(range(1, workers + 1))
    vectors = rng.standard_normal((workers, dimension), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vectors = vectors / norms

    service.index.build(worker_ids, [vector for vector in vectors])
    service.index.save(namespace="benchmark-load")

    latencies: list[float] = []
    for _ in range(probes):
        query = rng.standard_normal(dimension, dtype=np.float32)
        started = time.perf_counter()
        service.index.search(query, top_k=3)
        latencies.append((time.perf_counter() - started) * 1000.0)

    report = LoadBenchmarkReport(
        workers_indexed=workers,
        probes=probes,
        vector_dimension=dimension,
        avg_search_ms=_average(latencies),
        p95_search_ms=_p95(latencies),
        max_search_ms=max(latencies) if latencies else 0.0,
        active_embedder=service.status().active_embedder,
        active_index=service.status().active_index,
        output_file=str(benchmark_dir / "load_report.json"),
    )
    _write_report(Path(report.output_file), asdict(report))
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark the v2 facial recognition stack")
    subparsers = parser.add_subparsers(dest="command", required=True)

    dataset_parser = subparsers.add_parser("dataset", help="Run accuracy and latency benchmark on a folder dataset")
    dataset_parser.add_argument("--dataset-root", required=True, help="Folder with one subfolder per person")
    dataset_parser.add_argument("--enroll-per-person", type=int, default=3, help="Images per person used for enrollment")
    dataset_parser.add_argument("--max-people", type=int, default=100, help="Maximum people to benchmark")
    dataset_parser.add_argument("--min-images-per-person", type=int, default=5, help="Minimum images needed per person")
    dataset_parser.add_argument("--top-k", type=int, default=3, help="Recognition top-k search size")

    load_parser = subparsers.add_parser("load", help="Run search-speed benchmark without real face images")
    load_parser.add_argument("--workers", type=int, default=1000, help="Synthetic workers to index")
    load_parser.add_argument("--probes", type=int, default=250, help="Search probes to run")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "dataset":
        report = run_dataset_benchmark(
            dataset_root=Path(args.dataset_root),
            enroll_per_person=args.enroll_per_person,
            max_people=args.max_people,
            min_images_per_person=args.min_images_per_person,
            top_k=args.top_k,
        )
        print(json.dumps(asdict(report), indent=2))
        return

    if args.command == "load":
        report = run_load_benchmark(workers=args.workers, probes=args.probes)
        print(json.dumps(asdict(report), indent=2))
        return

    raise RuntimeError(f"Unknown command '{args.command}'.")


def _prepare_benchmark_environment(name: str) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    benchmark_dir = Path("data") / "benchmarks" / f"{name}-{timestamp}"
    benchmark_dir.mkdir(parents=True, exist_ok=True)
    os.environ["ATTENDANCE_DB_FILE"] = str(benchmark_dir / "benchmark.db")
    os.environ["ATTENDANCE_VECTOR_INDEX_FILE"] = str(benchmark_dir / "benchmark_index.npz")
    os.environ["ATTENDANCE_MATCH_CONFIRMATION_FRAMES"] = "1"
    os.environ["ATTENDANCE_MATCH_CONFIRMATION_WINDOW_SECONDS"] = "1"
    os.environ["ATTENDANCE_RECOGNITION_CACHE_TTL_SECONDS"] = "0"
    return benchmark_dir


def _build_service():
    service_module = importlib.import_module("src.v2.service")
    service_class = getattr(service_module, "ScalableAttendanceService")
    return service_class()


def _collect_people(dataset_root: Path, max_people: int, min_images_per_person: int) -> list[tuple[str, list[Path]]]:
    if not dataset_root.exists():
        raise RuntimeError(f"Dataset root does not exist: {dataset_root}")

    people: list[tuple[str, list[Path]]] = []
    for person_dir in sorted(dataset_root.iterdir()):
        if not person_dir.is_dir():
            continue
        images = [path for path in sorted(person_dir.iterdir()) if path.suffix.lower() in IMAGE_EXTENSIONS]
        if len(images) < min_images_per_person:
            continue
        people.append((person_dir.name.replace("_", " "), images))
        if len(people) >= max_people:
            break
    return people


def _split_detectable_images(service, images: list[Path], enroll_per_person: int) -> tuple[list[Path], list[Path]]:
    detectable: list[Path] = []
    rejected: list[Path] = []
    for image_path in images:
        try:
            detection = service.detect(image_path.read_bytes())
        except Exception:
            rejected.append(image_path)
            continue
        if detection.detected_faces > 0:
            detectable.append(image_path)
        else:
            rejected.append(image_path)

    enroll_paths = detectable[:enroll_per_person]
    test_paths = detectable[enroll_per_person:]
    return enroll_paths, test_paths


def _write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _average(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return round(values[0], 3)
    return round(float(statistics.quantiles(values, n=100)[94]), 3)


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 4)


if __name__ == "__main__":
    main()
