import os
from typing import List

import hydra
import matplotlib.pyplot as plt
import numpy as np
import torch
import torchvision
from omegaconf import DictConfig
from torchvision.datasets import CIFAR10

import src.utils as utils
from src.dataset import split_dataset
from src.plots import (
    plot_misclassification,
    plot_mismatch,
    plot_persistance,
    plot_stability,
)
from src.transform import cifar10_transform


@hydra.main(config_path="conf", config_name="plots")
def plots(cfg: DictConfig):
    utils.display_config(cfg)

    path = hydra.utils.to_absolute_path(cfg.preds_path)
    preds_a, preds_b = [], []
    epoch = 0

    while os.path.isfile(os.path.join(path, f"{epoch}.npy")):
        preds_a.append(np.load(os.path.join(path, f"{epoch}.npy")))
        preds_b.append(np.load(os.path.join(path, f"{epoch}_average.npy")))
        epoch += 1

    preds_a = np.stack(preds_a, axis=0)
    preds_b = np.stack(preds_b, axis=0)

    assert preds_a.shape == preds_b.shape, "Shape of predictions must be the same"

    labels = ["Single", "Averaged"]
    iters = np.arange(len(preds_a))

    plot_stability([preds_a, preds_b], labels, iters)
    plot_mismatch([preds_a, preds_b], labels, iters)
    plot_misclassification([preds_a, preds_b], labels, iters)
    top_n = plot_persistance([preds_a, preds_b], labels, iters, sort="stability")

    # get val set
    dataset = CIFAR10
    root = hydra.utils.to_absolute_path(cfg.dataset.root)

    val_set = dataset(
        root,
        train=True,
        transform=cifar10_transform(augment=False),
        download=cfg.dataset.download,
    )
    _, val_set = split_dataset(
        dataset=val_set,
        split=cfg.dataset.val.split,
        seed=cfg.dataset.val.seed,
    )

    # plot top n persistance samples
    os.makedirs("samples")
    for i in top_n:
        img, _ = val_set[i]
        torchvision.utils.save_image(img, f"samples/{i}.png")


if __name__ == "__main__":
    plots()