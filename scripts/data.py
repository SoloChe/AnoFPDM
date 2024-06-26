import torch
import random
from random import randint
from pathlib import Path
import numpy as np
import nibabel as nib
from tqdm import tqdm
import os
import logging
from torch.utils.data import (
    Dataset,
    DataLoader,
    ConcatDataset,
    WeightedRandomSampler,
    RandomSampler,
    DistributedSampler,
)
from torchvision import transforms, datasets
import torch.nn.functional as F
from torchvision.utils import make_grid, save_image
from torchvision import transforms, datasets
import torch.distributed as dist





# %% for BraTS dataset


# modification based on https://github.com/AntanasKascenas/DenoisingAE/blob/master/src/data.py
class PatientDataset(Dataset):
    """
    Dataset class representing a collection of slices from a single scan.
    """

    def __init__(
        self, patient_dir: Path, process_fun=None, id=None, skip_condition=None
    ):

        self.patient_dir = patient_dir
        # Make sure the slices are correctly sorted according to the slice number in case we want to assemble
        # "pseudo"-volumes later.
        self.slice_paths = sorted(
            list(patient_dir.iterdir()), key=lambda x: int(x.name[6:-4])
        )
        self.process = process_fun
        self.skip_condition = skip_condition
        self.id = id
        self.len = len(self.slice_paths)
        self.idx_map = {x: x for x in range(self.len)}

        if self.skip_condition is not None:

            # Try and find which slices should be skipped and thus determine the length of the dataset.
            valid_indices = []
            for idx in range(self.len):
                with np.load(self.slice_paths[idx]) as data:
                    if self.process is not None:
                        data = self.process(idx, **data)
                    if not skip_condition(data):
                        valid_indices.append(idx)

            self.len = len(valid_indices)
            self.idx_map = {x: valid_indices[x] for x in range(self.len)}

    def __getitem__(self, idx):
        idx = self.idx_map[idx]
        data = np.load(self.slice_paths[idx])
        if self.process is not None:
            data = self.process(idx, **data)
        return data

    def __len__(self):
        return self.len


class BrainDataset(Dataset):
    """
    Dataset class representing a collection of slices from scans from a specific dataset split.
    """

    def __init__(
        self,
        datapath,
        split="val",
        n_tumour_patients=None,
        n_healthy_patients=None,
        skip_healthy_s_in_tumour=False,  # whether to skip healthy slices in "tumour" patients
        skip_tumour_s_in_healthy=True,  # whether to skip tumour slices in healthy patients
        mixed=False,
        ret_lab=False,
        seed=0,
        num_mix=None,
    ):

        self.rng = random.Random(seed)

        assert split in ["train", "val", "test"]

        self.split = split
        self.ret_lab = ret_lab

        path = Path(datapath) / f"npy_{split}"

        # Slice skip conditions:
        threshold = 0
        self.skip_tumour = lambda item: item[1].sum() > threshold
        self.skip_healthy = lambda item: item[1].sum() <= threshold

        def process(idx, x, y):
            # treat all tumour classes as one for anomaly detection purposes.
            y = y > 0.5
            # x, y are 1x1x128x128 or x is 1x4x128x128 or 240x240
            x_tensor = torch.from_numpy(x[0]).float()
            y_tensor = torch.from_numpy(y[0]).float()

            if_tumor = torch.from_numpy(y[0]).float().sum() > 0
            lab = 1 if if_tumor else 0
            
            # rescacle to [-1, 1]
            x_min = x_tensor.view(x_tensor.shape[0], -1).min(1).values
            x_max = x_tensor.view(x_tensor.shape[0], -1).max(1).values
            x_tensor = (x_tensor - x_min[:, None, None]) / (
                x_max[:, None, None] - x_min[:, None, None] + 0.00001
            )  # [0, 1]
            x_tensor = x_tensor * 2 - 1  # [-1, 1]
            return x_tensor, y_tensor, lab

        patient_dirs = sorted(list(path.iterdir()))
        self.rng.shuffle(patient_dirs)

        
        if mixed: # take all slices 
            num_mix = len(patient_dirs) if num_mix is None else num_mix
            self.patient_datasets = [
                PatientDataset(
                    patient_dirs[i], process_fun=process, id=i, skip_condition=None
                )
                for i in range(num_mix)
            ]
        else: # take n_tumour_patients and n_healthy_patients
            assert (n_tumour_patients is not None) or (n_healthy_patients is not None)
            self.n_tumour_patients = (
                n_tumour_patients
                if n_tumour_patients is not None
                else len(patient_dirs)
            )
            self.n_healthy_patients = (
                n_healthy_patients
                if n_healthy_patients is not None
                else len(patient_dirs) - self.n_tumour_patients
            )

            self.patient_datasets = [
                PatientDataset(
                    patient_dirs[i],
                    process_fun=process,
                    id=i,
                    skip_condition=(
                        self.skip_healthy if skip_healthy_s_in_tumour else None
                    ),
                )
                for i in range(self.n_tumour_patients)
            ]

            # + only healthy slices from "healthy" patients
            self.patient_datasets += [
                PatientDataset(
                    patient_dirs[i],
                    process_fun=process,
                    id=i,
                    skip_condition=(
                        self.skip_tumour if skip_tumour_s_in_healthy else None
                    ),
                )
                for i in range(
                    self.n_tumour_patients,
                    self.n_tumour_patients + self.n_healthy_patients,
                )
            ]

        self.dataset = ConcatDataset(self.patient_datasets)

    def __getitem__(self, idx):
        x, gt, lab = self.dataset[idx]

        if self.split == "train" and not self.ret_lab:
            return x, {}
        elif self.split == "train" and self.ret_lab:
            return x, {"y": lab}
        elif self.split == "val" or self.split == "test":
            return x, gt, lab

    def __len__(self):
        return len(self.dataset)


# %% load brats
def load_brats(
    data_dir,
    split,
    n_healthy_patients=None,
    n_tumour_patients=None,
    mixed=False,
    ret_lab=False,
    num_mix=None,
):

    assert split in ["train", "val", "test"]

    if split == "train":
        return BrainDataset(
            data_dir,
            split="train",
            n_tumour_patients=n_tumour_patients,
            n_healthy_patients=n_healthy_patients,
            mixed=mixed,
            ret_lab=ret_lab,
        )
    else:
        return BrainDataset(
            data_dir,
            split=split,
            n_tumour_patients=n_tumour_patients,
            n_healthy_patients=n_healthy_patients,
            mixed=mixed,
            ret_lab=ret_lab,
            num_mix=num_mix,
        )


# %%
def get_brats_data_iter(
    data_dir,
    batch_size,
    split="train",
    ret_lab=False,
    logger=None,
    n_healthy_patients=None,
    n_tumour_patients=None,
    mixed=False,
    num_mix=None,
    seed=0,
):

    # torch.random.manual_seed(seed)
    rng = torch.Generator()
    rng.manual_seed(seed)

    data = load_brats(
        data_dir,
        split,
        n_healthy_patients,
        n_tumour_patients,
        mixed=mixed,
        ret_lab=ret_lab,
        num_mix=num_mix,
    )

    if split == "val": # for single GPU
        labels = [data[i][2] for i in range(len(data))]

        class_sample_count = np.array(
            [len(np.where(labels == t)[0]) for t in np.unique(labels)]
        )

        weight = 1.0 / class_sample_count
        samples_weight = np.array([weight[t] for t in labels])
        samples_weight = torch.from_numpy(samples_weight)
        samples_weight = samples_weight.double()
        replacement = True
        sampler = WeightedRandomSampler(
            samples_weight, len(samples_weight), replacement=replacement, generator=rng
        )
    elif split == "train":
        sampler = DistributedSampler(
            data,
            num_replicas=dist.get_world_size(),
            rank=dist.get_rank(),
            shuffle=True,
            seed=seed,
        )
    else:
        sampler = RandomSampler(data, generator=rng)

    loader = DataLoader(
        data,
        batch_size=int(batch_size // dist.get_world_size()) if split == "train" else batch_size,
        shuffle=False,
        sampler=sampler,
        num_workers=4,
        pin_memory=True,
        drop_last=True if split == 'train' else False,
    )

    if logger is not None:
        logger.log(f"data_size: {data.__len__()}")

    
    # if training:
    #     while True:
    #         yield from loader
    # else:
    #     yield from loader
    if split == "train":
        return loader, sampler
    return loader


def check_data(loader, image_dir, split="train", name="cifar10"):
    if split == "train":
        samples, _ = loader.__iter__().__next__()
    else:
        samples, gt, _ = loader.__iter__().__next__()

    samples_for_each_cls = 8

    if samples.shape[1] == 4:
        samples_for_each_cls = samples.shape[1]
        samples = samples.reshape(-1, 1, *samples.shape[2:])[:64]
        samples = (samples + 1) / 2

    images = make_grid(samples, nrow=samples_for_each_cls)

    os.makedirs(image_dir, exist_ok=True)

    save_image(images, os.path.join(image_dir, f"real_{name}_{split}.png"))
    if split != "train":
        save_image(
            make_grid(gt, nrow=samples_for_each_cls),
            os.path.join(image_dir, f"gt_{name}_{split}.png"),
        )


        

def get_data_iter(
    name,
    data_dir,
    batch_size,
    split="train",
    ret_lab=False,
    logger=None,
    kwargs=None,
):

    if name.lower() == "brats":
        return get_brats_data_iter(
            data_dir,
            batch_size,
            split=split,
            ret_lab=ret_lab,
            logger=logger,
            **kwargs,
        )
    elif name.lower() == "t1":
        pass
    else:
        raise NotImplementedError


if __name__ == "__main__":
  
    data_dir = "/data/amciilab/yiming/DATA/mmbrain/preprocessed_data_all_00_128"
    data = get_brats_data_iter(
        data_dir,
        128,
        split="test",
        training=False,
        # n_healthy_patients=None,
        # n_tumour_patients=None,
        mixed=True,
        ret_lab=True,
    )
    samples, gt, lab = next(data)
    print("batch shape: ", samples.shape)
    print("sample shape: ", samples[0].shape)
    print("gt: ", gt.shape)
    print("lab: ", lab.shape)
    print("lab: ", lab)

    # print('slice_num: ', slice_num[0])
    print("channel 1 max: ", samples[0][0].max())
    print("channel 1 min: ", samples[0][0].min())
    print('channel 2 max: ', samples[0][1].max())
    print('channel 2 min: ', samples[0][1].min())
    print('channel 3 max: ', samples[0][2].max())
    print('channel 3 min: ', samples[0][2].min())
    print('channel 4 max: ', samples[0][3].max())
    print('channel 4 min: ', samples[0][3].min())

    check_data(data, split="test", image_dir="./", name="mmbrain")
