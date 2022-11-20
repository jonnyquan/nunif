from PIL import Image, ImageCms, PngImagePlugin, UnidentifiedImageError
import io
import struct
import base64
import torchvision.transforms.functional as TF
from ..logger import logger


sRGB_profile = ImageCms.createProfile("sRGB")
CIE_Gray_profile = ImageCms.ImageCmsProfile(io.BytesIO(base64.b64decode("""
AAABqE95cmECMAAAbW50ckdSQVlMYWIgB9oACQABABUADAASYWNzcCpuaXg3FKy3bm9uZW5vbmX+
/v7/ZG1ubwAAAAAAAPbWAAEAAAAA0y1veXJhAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAFY3BydAAAAMAAAABFZGVzYwAAAQgAAABld3RwdAAAAXAAAAAUYmtw
dAAAAYQAAAAUa1RSQwAAAZgAAAAQdGV4dAAAAABDb3B5cmlnaHQgKEMpIDIwMDUtMjAxMCBLYWkt
VXdlIEJlaHJtYW5uIDx3d3cuYmVocm1hbm4ubmFtZT4AAAAAZGVzYwAAAAAAAAALR3JheSBDSUUq
TAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABYWVogAAAAAAAA9tYAAQAAAADTLVhZWiAAAAAAAAAA
AAAAAAAAAAAAY3VydgAAAAAAAAABAQAAAA==
"""))) #  from debian/icc-profiles-free/Gray-CIE_L.icc
GAMMA_LCD = 45454


def _load_image(im, filename, color=None, keep_alpha=False):
    meta = {"engine": "pil", "filename": filename}
    im.load()
    meta["mode"] = im.mode

    if im.mode == "I;16":
        im.mode = im.convert("L")
    if keep_alpha and im.mode in {"L", "I", "RGB", "P"}:
        transparency = im.info.get('transparency')
        if isinstance(transparency, bytes) or isinstance(transparency, int):
            if im.mode in {"RGB", "P"}:
                im = im.convert("RGBA")
            elif im.mode == "L":
                im = im.convert("LA")
    meta["icc_profile"] = im.info.get("icc_profile")
    if meta['icc_profile'] is not None:
        with io.BytesIO(meta['icc_profile']) as io_handle:
            # TODO: I'm not sure
            src_profile = ImageCms.ImageCmsProfile(io_handle)
            try:
                if im.mode == "CMYK":
                    im = ImageCms.profileToProfile(im, src_profile, sRGB_profile, outputMode="RGB")
                elif im.mode == "L":
                    im = ImageCms.profileToProfile(im, src_profile, CIE_Gray_profile, outputMode="L")
                elif im.mode == "LA":
                    alpha = im.getchannel("A")
                    im = im.convert("L")
                    im = ImageCms.profileToProfile(im, src_profile, CIE_Gray_profile, outputMode="L")
                    im.putalpha(alpha)
                else:
                    im = ImageCms.profileToProfile(im, src_profile, sRGB_profile)
            except ImageCms.PyCMSError as e:
                logger.warning(f"pil_io.load_image: profile error: {e}")

    if im.mode not in {"RGB", "RGBA", "L", "LA"}:
        im = im.convert("RGB")

    meta["grayscale"] = im.mode in {"L", "LA"}
    meta["gamma"] = None
    gamma = im.info.get("gamma")
    if gamma is not None:
        gamma = int(gamma * 100000)
        if gamma != 0 and gamma != GAMMA_LCD:
            meta["gamma"] = gamma

    if color is None:
        if im.mode in {"RGB", "RGBA"}:
            color = "rgb"
        else:
            color = "gray"
    if color == "rgb":
        if keep_alpha:
            if im.mode == "L":
                im = im.convert("RGB")
            elif im.mode == "LA":
                im = im.convert("RGBA")
        else:
            if im.mode != "RGB":
                im = im.convert("RGB")
    elif color == "gray":
        if keep_alpha:
            if im.mode == "RGB":
                im = im.convert("L")
            elif im.mode == "RGBA":
                im = im.convert("LA")
        else:
            if im.mode != "L":
                im = im.convert("L")

    return im, meta


def load_image(filename, color=None, keep_alpha=False):
    assert (color is None or color in {"rgb", "gray"})
    with open(filename, "rb") as f:
        try:
            im = Image.open(f)
            return _load_image(im, filename, color=color, keep_alpha=keep_alpha)
        except UnidentifiedImageError:
            return None, None


def decode_image(buff, filename=None, color=None, keep_alpha=False):
    with io.BytesIO(buff) as data:
        try:
            im = Image.open(data)
            return _load_image(im, filename, color=color, keep_alpha=keep_alpha)
        except UnidentifiedImageError:
            return None, None


def encode_image(im, format="png", meta=None,
                 compress_level=6):
    with io.BytesIO() as fp:
        save_image(im, fp, meta=meta, compress_level=compress_level)
        return fp.getvalue()


def to_tensor(im, return_alpha=False):
    alpha = None
    if im.mode == "RGBA":
        alpha = im.getchannel("A")
        im = im.convert("RGB")
    elif im.mode == "LA":
        alpha = im.getchannel("A")
        im = im.convert("L")

    x = TF.to_tensor(im)
    if return_alpha:
        if alpha is not None:
            alpha = TF.to_tensor(alpha)
        return x, alpha
    return x


def to_image(im, alpha=None):
    im = TF.to_pil_image(im)
    if alpha is not None:
        alpha = TF.to_pil_image(alpha)
        im.putalpha(alpha)
    return im


def save_image(im, filename, format="png",
               meta=None,
               compress_level=6):

    # TODO: support non PNG format

    pnginfo = PngImagePlugin.PngInfo()
    icc_profile = None
    if meta is not None:
        assert (meta["engine"] == "pil")

        if meta["icc_profile"] is not None:
            with io.BytesIO(meta['icc_profile']) as io_handle:
                # TODO: I'm not sure
                dst_profile = ImageCms.ImageCmsProfile(io_handle)
                try:
                    if meta["mode"] == "CMYK":
                        im = ImageCms.profileToProfile(im, sRGB_profile, dst_profile, outputMode="CMYK")
                        im = im.convert("RGB")
                    elif meta["mode"] == "L":
                        im = im.convert("L")
                        im = ImageCms.profileToProfile(im, CIE_Gray_profile, dst_profile, outputMode="L")
                    elif meta["mode"] == "LA":
                        alpha = im.getchannel("A")
                        im = im.convert("L")
                        im = ImageCms.profileToProfile(im, CIE_Gray_profile, dst_profile, outputMode="L")
                        im.putalpha(alpha)
                    else:
                        im = ImageCms.profileToProfile(im, sRGB_profile, dst_profile)
                    icc_profile = meta["icc_profile"]
                except ImageCms.PyCMSError as e:
                    logger.warning(f"pil_io.save_image: profile error: {e}")

        if meta["grayscale"]:
            if im.mode == "RGB":
                im = im.convert("L")
            elif im.mode == "RGBA":
                im = im.convert("LA")

        if meta["gamma"] is not None:
            pnginfo.add(b"gAMA", struct.pack(">I", meta["gamma"]))

    im.save(filename, format="png",
            icc_profile=icc_profile, pnginfo=pnginfo,
            compress_level=compress_level)