from __future__ import annotations

import argparse
import random
import shutil
from pathlib import Path

import cv2
import numpy as np


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a synthetic face dataset from existing face images")
    parser.add_argument(
        "--source-root",
        default=str(Path("data") / "faces"),
        help="Folder containing source person subfolders with real face images",
    )
    parser.add_argument(
        "--output-root",
        default=str(Path("data") / "synthetic_dataset"),
        help="Folder where synthetic identity folders will be created",
    )
    parser.add_argument("--identities", type=int, default=100, help="Number of synthetic identities to create")
    parser.add_argument("--images-per-identity", type=int, default=8, help="Images per synthetic identity")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible output")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete the output folder first if it already exists",
    )
    return parser


def generate_synthetic_dataset(
    source_root: Path,
    output_root: Path,
    identities: int,
    images_per_identity: int,
    seed: int,
    clean: bool,
) -> dict[str, int]:
    rng = random.Random(seed)
    np_rng = np.random.default_rng(seed)

    if clean and output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    source_images = _collect_source_images(source_root)
    if not source_images:
        raise RuntimeError(f"No source face images found under {source_root}")

    generated_images = 0
    for identity_index in range(1, identities + 1):
        person_dir = output_root / f"synthetic_{identity_index:03d}"
        person_dir.mkdir(parents=True, exist_ok=True)

        anchor = rng.choice(source_images)
        profile = _build_identity_profile(np_rng)
        for image_index in range(1, images_per_identity + 1):
            source_path = rng.choice(source_images if rng.random() < 0.25 else [anchor])
            image = cv2.imread(str(source_path), cv2.IMREAD_COLOR)
            if image is None:
                continue
            transformed = _apply_profile_transform(image, profile=profile, np_rng=np_rng, variation=image_index)
            output_path = person_dir / f"{image_index:02d}.jpg"
            cv2.imwrite(str(output_path), transformed, [int(cv2.IMWRITE_JPEG_QUALITY), 94])
            generated_images += 1

    return {
        "identities": identities,
        "images_per_identity": images_per_identity,
        "generated_images": generated_images,
    }


def _collect_source_images(source_root: Path) -> list[Path]:
    if not source_root.exists():
        return []
    images: list[Path] = []
    for path in source_root.rglob("*"):
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            images.append(path)
    return images


def _build_identity_profile(np_rng: np.random.Generator) -> dict[str, float]:
    return {
        "angle": float(np_rng.uniform(-12.0, 12.0)),
        "scale": float(np_rng.uniform(0.92, 1.08)),
        "shift_x": float(np_rng.uniform(-10.0, 10.0)),
        "shift_y": float(np_rng.uniform(-8.0, 8.0)),
        "brightness": float(np_rng.uniform(0.88, 1.14)),
        "contrast": float(np_rng.uniform(0.88, 1.18)),
        "gamma": float(np_rng.uniform(0.85, 1.15)),
        "blur": float(np_rng.uniform(0.0, 1.2)),
        "noise": float(np_rng.uniform(2.0, 10.0)),
        "vignette": float(np_rng.uniform(0.0, 0.22)),
        "tint_b": float(np_rng.uniform(-10.0, 10.0)),
        "tint_g": float(np_rng.uniform(-10.0, 10.0)),
        "tint_r": float(np_rng.uniform(-10.0, 10.0)),
    }


def _apply_profile_transform(
    image: np.ndarray,
    profile: dict[str, float],
    np_rng: np.random.Generator,
    variation: int,
) -> np.ndarray:
    working = image.copy()
    height, width = working.shape[:2]
    center = (width / 2.0, height / 2.0)
    angle = profile["angle"] + float(np_rng.uniform(-3.5, 3.5))
    scale = profile["scale"] + float(np_rng.uniform(-0.03, 0.03))
    shift_x = profile["shift_x"] + float(np_rng.uniform(-4.0, 4.0))
    shift_y = profile["shift_y"] + float(np_rng.uniform(-4.0, 4.0))

    matrix = cv2.getRotationMatrix2D(center, angle, scale)
    matrix[0, 2] += shift_x
    matrix[1, 2] += shift_y
    working = cv2.warpAffine(
        working,
        matrix,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT_101,
    )

    if variation % 5 == 0:
        working = cv2.flip(working, 1)

    alpha = profile["contrast"] + float(np_rng.uniform(-0.05, 0.05))
    beta = (profile["brightness"] - 1.0) * 60.0 + float(np_rng.uniform(-6.0, 6.0))
    working = cv2.convertScaleAbs(working, alpha=alpha, beta=beta)

    gamma = max(0.55, profile["gamma"] + float(np_rng.uniform(-0.05, 0.05)))
    gamma_table = np.array([((index / 255.0) ** (1.0 / gamma)) * 255 for index in range(256)]).astype(np.uint8)
    working = cv2.LUT(working, gamma_table)

    noise_sigma = profile["noise"] + float(np_rng.uniform(-1.5, 1.5))
    noise = np_rng.normal(0.0, noise_sigma, working.shape).astype(np.float32)
    noisy = np.clip(working.astype(np.float32) + noise, 0, 255)
    working = noisy.astype(np.uint8)

    blur_sigma = max(0.0, profile["blur"] + float(np_rng.uniform(-0.2, 0.2)))
    if blur_sigma > 0.2:
        working = cv2.GaussianBlur(working, (0, 0), blur_sigma)

    tint = np.array(
        [
            profile["tint_b"] + float(np_rng.uniform(-3.0, 3.0)),
            profile["tint_g"] + float(np_rng.uniform(-3.0, 3.0)),
            profile["tint_r"] + float(np_rng.uniform(-3.0, 3.0)),
        ],
        dtype=np.float32,
    )
    tinted = np.clip(working.astype(np.float32) + tint, 0, 255)
    working = tinted.astype(np.uint8)

    vignette_strength = max(0.0, profile["vignette"] + float(np_rng.uniform(-0.04, 0.04)))
    if vignette_strength > 0.02:
        working = _apply_vignette(working, vignette_strength)

    return working


def _apply_vignette(image: np.ndarray, strength: float) -> np.ndarray:
    rows, cols = image.shape[:2]
    kernel_x = cv2.getGaussianKernel(cols, cols * 0.45)
    kernel_y = cv2.getGaussianKernel(rows, rows * 0.45)
    mask = kernel_y @ kernel_x.T
    mask = mask / mask.max()
    mask = 1.0 - (1.0 - mask) * strength
    vignette = np.empty_like(image, dtype=np.float32)
    for channel in range(3):
        vignette[:, :, channel] = image[:, :, channel].astype(np.float32) * mask
    return np.clip(vignette, 0, 255).astype(np.uint8)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    summary = generate_synthetic_dataset(
        source_root=Path(args.source_root),
        output_root=Path(args.output_root),
        identities=args.identities,
        images_per_identity=args.images_per_identity,
        seed=args.seed,
        clean=args.clean,
    )
    print(summary)


if __name__ == "__main__":
    main()
