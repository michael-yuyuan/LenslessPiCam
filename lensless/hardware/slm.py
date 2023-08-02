# #############################################################################
# slm.py
# =================
# Authors :
# Eric BEZZAM [ebezzam@gmail.com]
# #############################################################################


import os
import numpy as np
from lensless.hardware.utils import check_username_hostname
from lensless.utils.io import get_dtype, get_ctypes
from slm_controller.hardware import SLMParam, slm_devices

from waveprop.spherical import spherical_prop
from waveprop.color import ColorSystem
from waveprop.rs import angular_spectrum
from waveprop.slm import get_centers, get_color_filter
from waveprop.devices import SLMParam as SLMParam_wp
from scipy.ndimage import rotate as rotate_func


try:
    import torch
    from torchvision import transforms

    torch_available = True
except ImportError:
    torch_available = False


SUPPORTED_DEVICE = {
    "adafruit": "~/slm-controller/examples/adafruit_slm.py",
    "nokia": "~/slm-controller/examples/nokia_slm.py",
    "holoeye": "~/slm-controller/examples/holoeye_slm.py",
}


def set_programmable_mask(pattern, device, rpi_username, rpi_hostname):
    """
    Set LCD pattern on Raspberry Pi.

    This function assumes that `slm-controller <https://github.com/ebezzam/slm-controller>`_
    is installed on the Raspberry Pi.

    Parameters
    ----------
    pattern : :py:class:`~numpy.ndarray`
        Pattern to set on programmable mask.
    device : str
        Name of device to set pattern on. Supported devices: "adafruit", "nokia", "holoeye".
    rpi_username : str
        Username of Raspberry Pi.
    rpi_hostname : str
        Hostname of Raspberry Pi.

    """

    client = check_username_hostname(rpi_username, rpi_hostname)

    # get path to python executable on Raspberry Pi
    rpi_python = "~/slm-controller/slm_controller_env/bin/python"
    assert (
        device in SUPPORTED_DEVICE.keys()
    ), f"Device {device} not supported. Supported devices: {SUPPORTED_DEVICE.keys()}"
    script = SUPPORTED_DEVICE[device]

    # check that pattern is correct shape
    expected_shape = slm_devices[device][SLMParam.SLM_SHAPE]
    if not slm_devices[device][SLMParam.MONOCHROME]:
        expected_shape = (3, *expected_shape)
    assert (
        pattern.shape == expected_shape
    ), f"Pattern shape {pattern.shape} does not match expected shape {expected_shape}"

    # save pattern
    pattern_fn = "tmp_pattern.npy"
    local_path = os.path.join(os.getcwd(), pattern_fn)
    np.save(local_path, pattern)

    # copy pattern to Raspberry Pi
    remote_path = f"~/{pattern_fn}"
    print(f"PUTTING {local_path} to {remote_path}")

    os.system('scp %s "%s@%s:%s" ' % (local_path, rpi_username, rpi_hostname, remote_path))
    # # -- not sure why this doesn't work... permission denied
    # sftp = client.open_sftp()
    # sftp.put(local_path, remote_path, confirm=True)
    # sftp.close()

    # run script on Raspberry Pi to set mask pattern
    command = f"{rpi_python} {script} --file_path {remote_path}"
    print(f"COMMAND : {command}")
    _stdin, _stdout, _stderr = client.exec_command(command)
    print(_stdout.read().decode())
    client.close()

    os.remove(local_path)


def get_programmable_mask(
    vals,
    sensor,
    slm_param,
    rotate=None,
    flipud=False,
    as_torch=False,
    torch_device="cpu",
    dtype=None,
    requires_grad=False,
):
    """
    Get mask as a numpy or torch array.

    """

    # determine data type
    use_torch = False
    if torch_available and as_torch:
        use_torch = True
    elif as_torch:
        print("Torch not available. Using numpy instead.")
        use_torch = False
    dtype = get_dtype(dtype, use_torch)

    # -- prepare SLM values
    slm_vals = vals.reshape((-1, vals.shape[-1]), order="F")

    if use_torch:
        slm_vals = torch.tensor(
            slm_vals, device=torch_device, dtype=dtype, requires_grad=requires_grad
        )
    n_active_slm_pixels = slm_vals.shape

    # -- prepare SLM mask
    n_color_filter = np.prod(slm_param["color_filter"].shape[:2])
    pixel_pitch = slm_param[SLMParam_wp.PITCH]
    centers = get_centers(n_active_slm_pixels, pixel_pitch=pixel_pitch)

    if SLMParam_wp.COLOR_FILTER in slm_param.keys():
        color_filter = slm_param[SLMParam_wp.COLOR_FILTER]
        if flipud:
            color_filter = np.flipud(color_filter)

        cf = get_color_filter(
            slm_dim=n_active_slm_pixels,
            color_filter=color_filter,
            shift=0,
            flat=True,
        )

    else:

        # monochrome
        cf = None

    d1 = sensor.pitch
    _height_pixel, _width_pixel = (slm_param[SLMParam_wp.CELL_SIZE] / d1).astype(int)

    if use_torch:
        mask = torch.zeros(
            (n_color_filter,) + tuple(sensor.resolution), dtype=dtype, device=torch_device
        )
        slm_vals_flat = slm_vals.flatten()
    else:
        mask = np.zeros((n_color_filter,) + tuple(sensor.resolution), dtype=dtype)
        slm_vals_flat = slm_vals.reshape(-1)

    for i, _center in enumerate(centers):

        _center_pixel = (_center / d1 + sensor.resolution / 2).astype(int)
        _center_top_left_pixel = (
            _center_pixel[0] - np.floor(_height_pixel / 2).astype(int),
            _center_pixel[1] + 1 - np.floor(_width_pixel / 2).astype(int),
        )

        if cf is not None:
            _rect = np.tile(cf[i][:, np.newaxis, np.newaxis], (1, _height_pixel, _width_pixel))
        else:
            _rect = np.ones((1, _height_pixel, _width_pixel))

        if use_torch:
            _rect = torch.tensor(_rect).to(slm_vals_flat)

        mask[
            :,
            _center_top_left_pixel[0] : _center_top_left_pixel[0] + _height_pixel,
            _center_top_left_pixel[1] : _center_top_left_pixel[1] + _width_pixel,
        ] = (
            slm_vals_flat[i] * _rect
        )

    # quantize mask to 255 levels
    if use_torch:
        mask = mask / torch.max(mask)
        mask = torch.round(mask * 255) / 255.0
    else:
        mask = mask / np.max(mask)
        mask = np.round(mask * 255) / 255.0

    # rotate
    if rotate is not None:
        if use_torch:
            mask = transforms.functional.rotate(mask, angle=rotate)
        else:
            mask = rotate_func(mask, axes=(2, 1), angle=rotate, reshape=False)

    return mask


def get_intensity_psf(
    mask,
    sensor,
    scene2mask,
    mask2sensor,
    color_system=None,
    waveprop=False,
    torch_device="cpu",
    dtype=None,
):
    if color_system is None:
        color_system = ColorSystem.rgb()

    if dtype is None:
        dtype = mask.dtype

    is_torch = False
    if torch_available:
        is_torch = isinstance(mask, torch.Tensor)

    dtype = get_dtype(dtype, is_torch)
    ctype, _ = get_ctypes(dtype, is_torch)

    if is_torch:
        psfs = torch.zeros(mask.shape, dtype=ctype, device=torch_device)
    else:
        psfs = np.zeros(mask.shape, dtype=ctype)

    if waveprop:

        assert (
            len(color_system.wv) == mask.shape[0]
        ), "Number of wavelengths must match number of color channels"

        # spherical wavefronts to mask
        spherical_wavefront = spherical_prop(
            in_shape=sensor.resolution,
            d1=sensor.pitch,
            wv=color_system.wv,
            dz=scene2mask,
            return_psf=True,
            is_torch=True,
            device=torch_device,
            dtype=dtype,
        )
        u_in = spherical_wavefront * mask

        # free space propagation to sensor
        for i, wv in enumerate(color_system.wv):
            psfs[i], _, _ = angular_spectrum(
                u_in=u_in[i],
                wv=wv,
                d1=sensor.pitch,
                dz=mask2sensor,
                dtype=dtype,
                device=torch_device,
            )

    else:

        psfs = mask

    # -- intensity PSF
    if is_torch:
        psf_in = torch.square(torch.abs(psfs))
    else:
        psf_in = np.square(np.abs(psfs))

    return psf_in
