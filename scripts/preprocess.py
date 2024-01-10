# Description: Preprocess the BraTS2021 dataset into numpy arrays
# adpated from https://github.com/AntanasKascenas/DenoisingAE/tree/master

import torch
import random
from pathlib import Path
import numpy as np
import nibabel as nib
from tqdm import tqdm

import torch.nn.functional as F


def normalise_percentile(volume):
    """
    Normalise the intensity values in each modality by scaling by 99 percentile foreground (nonzero) value.
    """
    for mdl in range(volume.shape[1]):
        v_ = volume[:, mdl, :, :].reshape(-1)
        v_ = v_[v_ > 0]  # Use only the brain foreground to calculate the quantile
        p_99 = torch.quantile(v_, 0.99)
        volume[:, mdl, :, :] /= p_99

    return volume


def process_patient(path, target_path, mod, pre=-1, post=-1, downsample=True):
    flair = nib.load(path / f"{path.name}_flair.nii.gz").get_fdata()
    t1 = nib.load(path / f"{path.name}_t1.nii.gz").get_fdata()
    t1ce = nib.load(path / f"{path.name}_t1ce.nii.gz").get_fdata()
    t2 = nib.load(path / f"{path.name}_t2.nii.gz").get_fdata()
    labels = nib.load(path / f"{path.name}_seg.nii.gz").get_fdata()

    assert mod in ["all", "flair", "t1", "t1ce", "t2"]

    # volume shape: [1, 1, h, w, slices]
    if mod == "all":
        volume = torch.stack([torch.from_numpy(x) for x in [flair, t1, t1ce, t2]], dim=0).unsqueeze(dim=0)
    elif mod == "flair":
        volume = torch.stack([torch.from_numpy(x) for x in [flair]], dim=0).unsqueeze(dim=0)
    elif mod == "t1":
        volume = torch.stack([torch.from_numpy(x) for x in [t1]], dim=0).unsqueeze(dim=0)
    elif mod == "t1ce":
        volume = torch.stack([torch.from_numpy(x) for x in [t1ce]], dim=0).unsqueeze(dim=0)
    elif mod == "t2":
        volume = torch.stack([torch.from_numpy(x) for x in [t2]], dim=0).unsqueeze(dim=0)

    # exclude first n and last m slices
    # 1 4 240 240 155; 240 240 155
    
    if pre > 0 and post > 0:
        volume = volume[:, :, :, :, pre:-post]
        labels = labels[:, :, pre:-post]
    elif pre > 0 and post < 0:
        volume = volume[:, :, :, :, pre:]
        labels = labels[:, :, pre:]
    elif pre < 0 and post > 0:
        volume = volume[:, :, :, :, :-post]
        labels = labels[:, :, :-post]
        
    # 1 1 240 240 155
    labels = torch.from_numpy(labels > 0.5).float().unsqueeze(dim=0).unsqueeze(dim=0)

    patient_dir = target_path / f"patient_{path.name}"
    patient_dir.mkdir(parents=True, exist_ok=True)

    volume = normalise_percentile(volume)

    sum_dim2 = (volume[0].mean(dim=0).sum(axis=0).sum(axis=0) > 0.5).int()
    fs_dim2 = sum_dim2.argmax()
    ls_dim2 = volume[0].mean(dim=0).shape[2] - sum_dim2.flip(dims=[0]).argmax()

    print(f"Patient {path.name} has {fs_dim2} to {ls_dim2} slices with brain tissue.", flush=True)
    
    for slice_idx in range(fs_dim2, ls_dim2):
        if downsample:
            low_res_x = F.interpolate(volume[:, :, :, :, slice_idx], mode="bilinear", size=(128, 128))
            low_res_y = F.interpolate(labels[:, :, :, :, slice_idx], mode="bilinear", size=(128, 128))
        else:
            low_res_x = volume[:, :, :, :, slice_idx]
            low_res_y = labels[:, :, :, :, slice_idx]
        np.savez_compressed(patient_dir / f"slice_{slice_idx}", x=low_res_x, y=low_res_y)


def preprocess(datapath: Path, mod: str, pre=-1, post=-1):

    all_imgs = sorted(list((datapath).iterdir()))

    splits_path = datapath.parent / f"preprocessed_data_{mod}_{pre}{post}" / "data_splits"

    if not splits_path.exists():

        indices = list(range(len(all_imgs)))
        random.seed(10)
        random.shuffle(indices)

        n_train = int(len(indices) * 0.80)
        n_val = int(len(indices) * 0.05)
        n_test = len(indices) - n_train - n_val

        split_indices = {}
        split_indices["train"] = indices[:n_train]
        split_indices["val"] = indices[n_train:n_train + n_val]
        split_indices["test"] = indices[n_train + n_val:]

        for split in ["train", "val", "test"]:
            (splits_path / split).mkdir(parents=True, exist_ok=True)
            with open(splits_path / split / "scans.csv", "w") as f:
                f.write("\n".join([all_imgs[idx].name for idx in split_indices[split]]))

    for split in ["train", "val", "test"]:
        paths = [datapath / x.strip() for x in open(splits_path / split / "scans.csv").readlines()]

        print(f"Patients in {split}]: {len(paths)}")

        for source_path in tqdm(paths):
            target_path = datapath.parent / f"preprocessed_data_{mod}_{pre}{post}" / f"npy_{split}"
            process_patient(source_path, target_path, mod, pre, post, downsample=False)


if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--source", default='/data/amciilab/yiming/DATA/BraTS21_training/BraTS21', type=str, help="path to Brats2021 Data directory")
    parser.add_argument("-m", "--mod", default='flair', type=str, help="modelity to preprocess")
    
    parser.add_argument("--pre", default=-1, 
                        type=int, help="skip first n slices")
    parser.add_argument("--post", default=-1,
                        type=int, help="skip last n slices")
    
    args = parser.parse_args()

    datapath = Path(args.source)
    mod = args.mod

    preprocess(datapath, mod, args.pre, args.post)
