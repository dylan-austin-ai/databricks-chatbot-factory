"""Drift math (OBS-12). Pure numpy so it unit-tests anywhere."""
from __future__ import annotations

import numpy as np


def pca_2d(x: np.ndarray) -> np.ndarray:
    """Project embeddings to 2-D (top two principal components) for the question map."""
    centered = x - x.mean(axis=0)
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    return centered @ vt[:2].T


def kmeans(x: np.ndarray, k: int, iters: int = 25, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Question topics (OBS-20): k-means on normalized embeddings. Returns (labels, centroids)."""
    x = x / np.maximum(np.linalg.norm(x, axis=1, keepdims=True), 1e-9)
    k = max(1, min(k, len(x)))
    c = x[np.random.default_rng(seed).choice(len(x), k, replace=False)]
    for _ in range(iters):
        labels = np.argmax(x @ c.T, axis=1)
        c = np.stack([x[labels == j].mean(axis=0) if (labels == j).any() else c[j] for j in range(k)])
        c /= np.maximum(np.linalg.norm(c, axis=1, keepdims=True), 1e-9)
    return np.argmax(x @ c.T, axis=1), c


def topic_count(n: int) -> int:
    """About one topic per 60 questions, between 2 and 12."""
    return max(2, min(12, n // 60))
