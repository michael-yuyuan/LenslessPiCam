"""

Download subset from here: https://drive.switch.ch/index.php/s/vmAZzryGI8U8rcE
Or full dataset here: https://github.com/Waller-Lab/LenslessLearning

To use integrated test function place the downloaded folder inside the data folder.
"""

import glob
import os
import pathlib as plib
from datetime import datetime
from lensless.io import load_psf
import numpy as np
from tqdm import tqdm

try:
    import torch
    from torch.utils.data import Dataset, DataLoader
    from torch.nn import MSELoss, L1Loss
    from torchmetrics import StructuralSimilarityIndexMeasure
    from torchmetrics.image import lpip, psnr
except ImportError:
    raise ImportError("Torch and torchmetrics are needed to benchmark reconstruction algorithm")


class ParallelDataset(Dataset):
    """
    Dataset consisting on mesure images with both a lensless and a lensed setup.
    """

    def __init__(
        self,
        root_dir,
        n_files=False,
        background=None,
        transform_lensless=None,
        transform_lensed=None,
    ):
        """

        Parameters
        ----------
        root_dir : str
            Path to the test dataset.
            It is expected to contained a file psf.tiff and two folder :
              diffuser and lensed containing each pair of test image (lensless and lensed) with the same name as a .npy file.
        n_files : int or None, optional
            Metrics will be computed only on the first n_files images.
            If None, all images are used, by default False
        background : torch.Tensor or None, optional
            If not None, background is removed from lensless images, by default None
        transform_lensless : torch.Transform or None, optional
            Transform to apply to the lensless images, by default None
        transform_lensed : torch.Transform or None, optional
            Transform to apply to the lensed images, by default None
        """

        self.root_dir = root_dir
        self.lensless_dir = os.path.join(root_dir, "diffuser")
        self.lensed_dir = os.path.join(root_dir, "lensed")
        files = glob.glob(self.lensless_dir + "/*.npy")
        print(self.lensless_dir)
        if n_files:
            files = files[:n_files]
        self.files = [os.path.basename(fn) for fn in files]

        self.background = background
        self.transform_lensless = transform_lensless
        self.transform_lensed = transform_lensed

    def __len__(self):
        return len(self.files)

    def __getitem__(self, idx):
        if torch.is_tensor(idx):
            idx = idx.tolist()

        lensless_fp = os.path.join(self.lensless_dir, self.files[idx])
        lensed_fp = os.path.join(self.lensed_dir, self.files[idx])
        lensless = torch.from_numpy(np.load(lensless_fp))
        lensed = torch.from_numpy(np.load(lensed_fp))

        if self.background is not None:
            lensless = lensless - self.background

        if self.transform_lensless:
            lensless = self.transform_lensless(lensless)

        if self.transform_lensed:
            lensed = self.transform_lensed(lensed)

        return lensless, lensed


def benchmark(model, data, downsample=4, n_files=100, batchsize=1, **kwargs):
    """
    Compute multiple metrics for a reconstruction algorithm.

    Parameters
    ----------
    model : class:ReconstructionAlgorithm
        Reconstruction algorithm to benchmark
    data : str
        Path to the test dataset.
        It is expected to contained a file psf.tiff and two folder :
          diffuser and lensed containing each pair of test image (lensless and lensed) with the same name as a .npy file.
    downsample : int, optional
        By how mush the psf and image should be downsample, by default 4
    n_files : int, optional
        Metrics will be computed only on the first n_files images, by default 100
    batchsize : int, optional
        Batch size for processing. For maximum compatibility use 1 (batchsize above 1 are not supported on all algorithm), by default 1

    Returns
    -------
    Dict[str, float]
        A dictionnary containing the metrics name and average value
    """
    assert isinstance(model._psf, torch.Tensor), "model need to be constructed with torch support"
    device = model._psf.device

    psf_fp = os.path.join(data, "psf.tiff")
    psf_float, background = load_psf(
        psf_fp,
        downsample=downsample,
        return_float=True,
        return_bg=True,
        bg_pix=(0, 15),
    )
    background = torch.from_numpy(background)

    dataset = ParallelDataset(data, n_files=n_files, background=background)
    dataloader = DataLoader(dataset, batch_size=batchsize, pin_memory=(device != "cpu"))

    metrics = {
        "MSE": MSELoss().to(device),
        "MAE": L1Loss().to(device),
        "LPIPS": lpip.LearnedPerceptualImagePatchSimilarity(net_type="vgg").to(device),
        "PSNR": psnr.PeakSignalNoiseRatio().to(device),
        "SSIM": StructuralSimilarityIndexMeasure().to(device),
    }
    metrics_values = {key: 0.0 for key in metrics}

    for lensless, lensed in tqdm(dataloader):
        lensless = lensless.to(device).squeeze()
        lensed = lensed.to(device).permute(0, 3, 1, 2)

        # compute predictions
        with torch.no_grad():
            if batchsize == 1:
                model.set_data(lensless)
                prediction = model.apply(plot=False, save=False, **kwargs)[None, :, :, :].permute(
                    0, 3, 1, 2
                )
            else:
                prediction = model.batch_call(plot=False, save=False, **kwargs).permute(0, 3, 1, 2)

        # normalization
        prediction_max = torch.amax(prediction, dim=(1, 2, 3), keepdim=True)
        prediction = prediction / prediction_max
        lensed_max = torch.amax(lensed, dim=(1, 2, 3), keepdim=True)
        lensed = lensed / lensed_max
        # compute metrics
        for metric in metrics:
            metrics_values[metric] += metrics[metric](prediction, lensed).cpu().item()

    # average metrics
    for metric in metrics:
        metrics_values[metric] /= len(dataloader)

    return metrics_values


if __name__ == "__main__":
    from lensless import ADMM

    downsample = 4
    device = "cpu"

    data = "data/DiffuserCam_Mirflickr_200_3011302021_11h43_seed11"
    psf_fp = os.path.join(data, "psf.tiff")
    psf_float, background = load_psf(
        psf_fp,
        downsample=downsample,
        return_float=True,
        return_bg=True,
        bg_pix=(0, 15),
    )
    psf = torch.from_numpy(psf_float).to(device)
    model = ADMM(psf)
    print(benchmark(model, data, n_files=10, downsample=downsample, n_iter=100))
