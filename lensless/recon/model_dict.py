"""
First key is camera, second key is training data, third key is model name.

Download link corresponds to output folder from training
script, which contains the model checkpoint and config file,
and other intermediate files. Models are stored on Hugging Face.
"""

import os
import numpy as np
import yaml
import torch
from lensless.recon.utils import create_process_network
from lensless.recon.unrolled_admm import UnrolledADMM
from huggingface_hub import snapshot_download
from lensless.hardware.trainable_mask import prep_trainable_mask


model_dir_path = os.path.join(os.path.dirname(__file__), "..", "..", "models")

model_dict = {
    "digicam": {
        "celeba_26k": {
            "unrolled_admm10": "bezzam/digicam-celeba-unrolled-admm10",
            "unrolled_admm10_ft_psf": "bezzam/digicam-celeba-unrolled-admm10-ft-psf",
            "unet8M": "bezzam/digicam-celeba-unet8M",
            "unrolled_admm10_post8M": "bezzam/digicam-celeba-unrolled-admm10-post8M",
            "unrolled_admm10_ft_psf_post8M": "bezzam/digicam-celeba-unrolled-admm10-ft-psf-post8M",
            "pre8M_unrolled_admm10": "bezzam/digicam-celeba-pre8M-unrolled-admm10",
            "pre4M_unrolled_admm10_post4M": "bezzam/digicam-celeba-pre4M-unrolled-admm10-post4M",
            "pre4M_unrolled_admm10_post4M_OLD": "bezzam/digicam-celeba-pre4M-unrolled-admm10-post4M_OLD",
            "pre4M_unrolled_admm10_ft_psf_post4M": "bezzam/digicam-celeba-pre4M-unrolled-admm10-ft-psf-post4M",
            # baseline benchmarks which don't have model file but use ADMM
            "admm_measured_psf": "bezzam/digicam-celeba-admm-measured-psf",
            "admm_simulated_psf": "bezzam/digicam-celeba-admm-simulated-psf",
        }
    }
}


def download_model(camera, dataset, model):

    """
    Download model from model_dict (if needed).

    Parameters
    ----------
    dataset : str
        Dataset used for training.
    model_name : str
        Name of model.
    """

    if camera not in model_dict:
        raise ValueError(f"Camera {camera} not found in model_dict.")

    if dataset not in model_dict[camera]:
        raise ValueError(f"Dataset {dataset} not found in model_dict.")

    if model not in model_dict[camera][dataset]:
        raise ValueError(f"Model {model} not found in model_dict.")

    repo_id = model_dict[camera][dataset][model]
    model_dir = os.path.join(model_dir_path, camera, dataset, model)

    if not os.path.exists(model_dir):
        snapshot_download(repo_id=repo_id, local_dir=model_dir)

    return model_dir


def load_model(model_path, psf, device):
    """
    Load best model from model path.

    Parameters
    ----------
    model_path : str
        Path to model.
    psf : py:class:`~torch.Tensor`
        PSF tensor.
    device : str
        Device to load model on.
    """

    # load config
    config_path = os.path.join(model_path, ".hydra", "config.yaml")
    with open(config_path, "r") as stream:
        config = yaml.safe_load(stream)

    # TODO : quick fix
    if config["trainable_mask"]["initial_value"].endswith("npy"):
        config["trainable_mask"][
            "initial_value"
        ] = "/home/bezzam/LenslessPiCam/adafruit_random_pattern_20231004_174047.npy"

    # check if trainable mask
    downsample = (
        config["files"]["downsample"] * 4
    )  # measured files are 4x downsampled (TODO, maybe celeba only?)
    mask = prep_trainable_mask(config, psf, downsample=downsample)
    if mask is not None:
        # if config["trainable_mask"]["mask_type"] is not None:
        # load best mask setting and update PSF

        if config["trainable_mask"]["mask_type"] == "AdafruitLCD":
            # -- load best values
            mask_vals = np.load(os.path.join(model_path, "mask_epochBEST.npy"))
            cf_path = os.path.join(model_path, "mask_color_filter_epochBEST.npy")
            if os.path.exists(cf_path):
                cf = np.load(cf_path)
            else:
                cf = None

            # -- set values and get new PSF
            with torch.no_grad():
                mask._mask = torch.nn.Parameter(torch.tensor(mask_vals, device=device))
                if cf is not None:
                    mask.color_filter = torch.nn.Parameter(torch.tensor(cf, device=device))
                psf = mask.get_psf().to(device)

        else:

            raise NotImplementedError

    # load best model
    model_checkpoint = os.path.join(model_path, "recon_epochBEST")
    model_state_dict = torch.load(model_checkpoint, map_location=device)

    pre_process = None
    post_process = None

    if "skip_unrolled" not in config["reconstruction"].keys():
        config["reconstruction"]["skip_unrolled"] = False

    if config["reconstruction"]["pre_process"]["network"] is not None:

        pre_process, _ = create_process_network(
            network=config["reconstruction"]["pre_process"]["network"],
            depth=config["reconstruction"]["pre_process"]["depth"],
            nc=config["reconstruction"]["pre_process"]["nc"]
            if "nc" in config["reconstruction"]["pre_process"].keys()
            else None,
            device=device,
        )

    if config["reconstruction"]["post_process"]["network"] is not None:

        post_process, _ = create_process_network(
            network=config["reconstruction"]["post_process"]["network"],
            depth=config["reconstruction"]["post_process"]["depth"],
            nc=config["reconstruction"]["post_process"]["nc"]
            if "nc" in config["reconstruction"]["post_process"].keys()
            else None,
            device=device,
        )

    if config["reconstruction"]["method"] == "unrolled_admm":
        recon = UnrolledADMM(
            psf,
            pre_process=pre_process,
            post_process=post_process,
            n_iter=config["reconstruction"]["unrolled_admm"]["n_iter"],
            skip_unrolled=config["reconstruction"]["skip_unrolled"],
        )

        recon.load_state_dict(model_state_dict)
    else:
        raise ValueError(
            f"Reconstruction method {config['reconstruction']['method']} not supported."
        )

    return recon
