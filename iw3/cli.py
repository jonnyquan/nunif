import os
import sys
from os import path
import torch
import torch.nn.functional as F
import torchvision.transforms.functional as TF
from concurrent.futures import ThreadPoolExecutor as PoolExecutor
import argparse
from tqdm import tqdm
import mimetypes
from PIL import Image
from nunif.utils.image_loader import ImageLoader
from nunif.utils.pil_io import load_image_simple
from nunif.utils.seam_blending import SeamBlending
from nunif.models import load_model, get_model_device
from nunif.device import create_device
from .utils import normalize_depth, make_input_tensor, batch_infer
import nunif.utils.video as VU
from . import models # noqa


def apply_divergence_grid_sample(c, depth, divergence, shift):
    w, h = c.shape[2], c.shape[1]
    index_shift = (1. - depth ** 2) * (shift * divergence * 0.01)
    mesh_y, mesh_x = torch.meshgrid(torch.linspace(-1, 1, h), torch.linspace(-1, 1, w))
    mesh_x = mesh_x - index_shift
    grid = torch.stack((mesh_x, mesh_y), 2)
    z = F.grid_sample(c.unsqueeze(0), grid.unsqueeze(0),
                      mode="bicubic", padding_mode="border", align_corners=True)
    z = z.squeeze(0)
    z = torch.clamp(z, 0., 1.)
    return z


def apply_divergence_nn(model, c, depth, divergence, shift, batch_size=64):
    device = get_model_device(model)
    enable_amp = "cuda" in str(device)
    image_width = c.shape[2]
    depth_min, depth_max = depth.min(), depth.max()
    if shift > 0:
        c = torch.flip(c, (2,))
        depth = torch.flip(depth, (2,))

    def config_callback(x):
        return 7, x.shape[1], x.shape[2]

    def preprocess_callback(_, pad):
        xx = F.pad(c.unsqueeze(0), pad, mode="replicate").squeeze(0)
        dd = F.pad(depth.float().unsqueeze(0), pad, mode="replicate").squeeze(0)
        return (xx, dd)

    def input_callback(p, i1, i2, j1, j2):
        xx = p[0][:, i1:i2, j1:j2]
        dd = p[1][:, i1:i2, j1:j2]
        return make_input_tensor(xx, dd,
                                 # apply divergence scale to image width instead of divergence
                                 divergence=model.fixed_divergence,
                                 image_width=image_width * (divergence / model.fixed_divergence),
                                 depth_min=depth_min, depth_max=depth_max)

    z = SeamBlending.tiled_render(c, model, tile_size=256, batch_size=batch_size, enable_amp=enable_amp,
                                  config_callback=config_callback,
                                  preprocess_callback=preprocess_callback,
                                  input_callback=input_callback)
    if shift > 0:
        z = torch.flip(z, (2,))
    return z


class HiddenPrints:
    def __enter__(self):
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        sys.stdout = open(os.devnull, 'w')
        sys.stderr = open(os.devnull, 'w')

    def __exit__(self, exc_type, exc_val, exc_tb):
        sys.stdout.close()
        sys.stderr.close()
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr


def load_depth_model(model_type="ZoeD_N", gpu=0):
    with HiddenPrints():
        model = torch.hub.load("isl-org/ZoeDepth:main", model_type, config_mode="infer",
                               pretrained=True, verbose=False)
    device = create_device(gpu)
    model = model.to(device).eval()
    return model


def update_model():
    # See https://github.com/isl-org/ZoeDepth/blob/main/hubconf.py
    # Triggers fresh download of MiDaS repo
    torch.hub.help("isl-org/MiDaS", "DPT_BEiT_L_384", force_reload=True)


# Filename suffix for VR Player's video format detection
# LRF: full left-right 3D video
SBS_SUFFIX = "_LRF"


# SMB Invalid characters
# Linux SMB replaces file names with random strings if they contain these invalid characters
# So need to remove these for the filenaming rules.
SMB_INVALID_CHARS = '\\/:*?"<>|'


def make_output_filename(input_filename, video=False):
    basename = path.splitext(path.basename(input_filename))[0]
    basename = basename.translate({ord(c): ord("_") for c in SMB_INVALID_CHARS})
    return basename + SBS_SUFFIX + (".mp4" if video else ".png")


def save_image(im, output_filename):
    im.save(output_filename)


def get_output_size(width, height, pad=None, rotate=False):
    if rotate:
        width, height = height, width
    if pad is not None:
        pad_h = int(height * pad)
        pad_h -= pad_h % 2
        pad_w = int(width * pad) // 2
        width = width + pad_w * 2
        height = height + pad_h
    return width, height


def remove_bg_from_image(im, bg_session):
    # TODO: mask resolution seems to be low
    mask = TF.to_tensor(rembg.remove(im, session=bg_session, only_mask=True))
    im = TF.to_tensor(im)
    bg_color = torch.tensor((0.4, 0.4, 0.2)).view(3, 1, 1)
    im = im * mask + bg_color * (1.0 - mask)
    im = TF.to_pil_image(im)

    return im


def process_image(im, args, depth_model, side_model):
    with torch.inference_mode():
        if args.rotate_left:
            im = im.transpose(Image.Transpose.ROTATE_90)
        elif args.rotate_right:
            im = im.transpose(Image.Transpose.ROTATE_270)
        im_org = TF.to_tensor(im)
        if args.bg_session is not None:
            im = remove_bg_from_image(im, args.bg_session)
        if args.disable_zoedepth_batch:
            depth = TF.to_tensor(depth_model.infer_pil(im, output_type="pil"))
        else:
            depth = batch_infer(depth_model, im)
        if args.method == "grid_sample":
            depth = normalize_depth(depth.squeeze(0))
            left_eye = apply_divergence_grid_sample(im_org, depth, args.divergence, shift=-1)
            right_eye = apply_divergence_grid_sample(im_org, depth, args.divergence, shift=1)
        else:
            left_eye = apply_divergence_nn(side_model, im_org, depth, args.divergence,
                                           shift=-1, batch_size=args.batch_size)
            right_eye = apply_divergence_nn(side_model, im_org, depth, args.divergence,
                                            shift=1, batch_size=args.batch_size)
        if args.pad is not None:
            pad_h = int(left_eye.shape[1] * args.pad)
            pad_h -= pad_h % 2
            pad_w = int(left_eye.shape[2] * args.pad) // 2
            left_eye = TF.pad(left_eye, (pad_w, pad_h, pad_w, 0), padding_mode="constant")
            right_eye = TF.pad(right_eye, (pad_w, pad_h, pad_w, 0), padding_mode="constant")
        sbs = torch.cat([left_eye, right_eye], dim=2)
        sbs = TF.to_pil_image(sbs)
        return sbs


def process_images(args, depth_model, side_model):
    os.makedirs(args.output, exist_ok=True)
    loader = ImageLoader(
        directory=args.input,
        load_func=load_image_simple,
        load_func_kwargs={"color": "rgb"})
    futures = []
    with PoolExecutor(max_workers=4) as pool:
        for im, meta in tqdm(loader, ncols=80):
            filename = meta["filename"]
            output_filename = path.join(args.output, make_output_filename(filename))
            if im is None or (args.resume and path.exists(output_filename)):
                continue
            output = process_image(im, args, depth_model, side_model)
            f = pool.submit(save_image, output, output_filename)
            #  f.result() # for debug
            futures.append(f)
        for f in futures:
            f.result()


def process_video_full(args, depth_model, side_model):
    def config_callback(stream):
        fps = VU.get_fps(stream)
        if float(fps) > args.max_fps:
            fps = args.max_fps
        width, height = get_output_size(
            stream.codec_context.width,
            stream.codec_context.height,
            pad=args.pad,
            rotate=args.rotate_left or args.rotate_right)

        options = {"preset": args.preset, "crf": str(args.crf)}
        tune = []
        if fps < 2:
            tune += ["stillimage"]
        if args.tune:
            tune += args.tune
        tune = set(tune)
        if tune:
            options["tune"] = ",".join(tune)
        return VU.VideoOutputConfig(
            width * 2, height,
            fps=fps,
            options=options
        )

    def frame_callback(frame):
        return frame.from_image(process_image(frame.to_image(), args, depth_model, side_model))

    if path.isdir(args.output) or "." not in path.basename(args.output):
        os.makedirs(args.output, exist_ok=True)
        output_filename = path.join(args.output, make_output_filename(path.basename(args.input), video=True))
    else:
        output_filename = args.output

    if args.resume and path.exists(output_filename):
        return

    if not args.yes and path.exists(output_filename):
        y = input(f"File '{output_filename}' already exists. Overwrite? [y/N]").lower()
        if y not in {"y", "ye", "yes"}:
            return

    VU.process_video(args.input, output_filename,
                     config_callback=config_callback,
                     frame_callback=frame_callback,
                     vf=args.vf)


def process_video_keyframes(args, depth_model, side_model):
    if path.isdir(args.output) or "." not in path.basename(args.output):
        os.makedirs(args.output, exist_ok=True)
        output_dir = path.join(args.output, make_output_filename(path.basename(args.input), video=True))
    else:
        output_dir = args.output
    output_dir = path.join(path.dirname(output_dir), path.splitext(path.basename(output_dir))[0])
    if output_dir.endswith("_LRF"):
        output_dir = output_dir[:-4]
    os.makedirs(output_dir, exist_ok=True)
    with PoolExecutor(max_workers=4) as pool:
        futures = []

        def frame_callback(frame):
            output = process_image(frame.to_image(), args, depth_model, side_model)
            output_filename = path.join(
                output_dir,
                path.basename(output_dir) + "_" + str(frame.index).zfill(8) + SBS_SUFFIX + ".png")
            f = pool.submit(save_image, output, output_filename)
            futures.append(f)
        VU.process_video_keyframes(args.input, frame_callback=frame_callback,
                                   min_interval_sec=args.keyframe_interval)
        for f in futures:
            f.result()


def process_video(args, depth_model, side_model):
    if args.keyframe:
        process_video_keyframes(args, depth_model, side_model)
    else:
        process_video_full(args, depth_model, side_model)


FLOW_D25_MODEL_PATH = path.join(path.dirname(__file__), "pretrained_models", "row_flow_d25.pth")
FLOW_D20_MODEL_PATH = path.join(path.dirname(__file__), "pretrained_models", "row_flow_d20.pth")


def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    if torch.cuda.is_available() or torch.backends.mps.is_available():
        default_gpu = 0
    else:
        default_gpu = -1

    parser.add_argument("--input", "-i", type=str, required=True,
                        help="input file or directory")
    parser.add_argument("--output", "-o", type=str, required=True,
                        help="output file or directory")
    parser.add_argument("--gpu", "-g", type=int, default=default_gpu,
                        help="GPU device id. -1 for CPU")
    parser.add_argument("--method", type=str, default="row_flow",
                        choices=["grid_sample", "row_flow"],
                        help="left-right divergence method")
    parser.add_argument("--divergence", "-d", type=float, default=2.0,
                        help=("strength of 3D effect"))
    parser.add_argument("--update", action="store_true",
                        help="force update midas models from torch hub")
    parser.add_argument("--resume", action="store_true",
                        help="skip processing when output file is already exist")
    parser.add_argument("--batch-size", type=int, default=64,
                        help="batch size for 256x256 tiled input")
    parser.add_argument("--max-fps", type=float, default=30,
                        help="max framerate for video. output fps = min(fps, --max-fps)")
    parser.add_argument("--crf", type=int, default=20,
                        help="constant quality value for video. smaller value is higher quality")
    parser.add_argument("--preset", type=str, default="ultrafast",
                        choices=["ultrafast", "superfast", "veryfast", "faster", "fast",
                                 "medium", "slow", "slower", "veryslow", "placebo"],
                        help="encoder preset option for video")
    parser.add_argument("--tune", type=str, nargs="+", default=["zerolatency"],
                        choices=["film", "animation", "grain", "stillimage",
                                 "fastdecode", "zerolatency"],
                        help="encoder tunings option for video")
    parser.add_argument("--yes", "-y", action="store_true", default=False,
                        help="overwrite output files")
    parser.add_argument("--pad", type=float, help="pad_size = int(size * pad)")
    parser.add_argument("--depth-model", type=str, default="ZoeD_N",
                        choices=["ZoeD_N", "ZoeD_K", "ZoeD_NK"],
                        help="depth model name")
    parser.add_argument("--remove-bg", action="store_true",
                        help="remove background depth, not recommended for video")
    parser.add_argument("--bg-model", type=str, default="u2net_human_seg",
                        help="rembg model type")
    parser.add_argument("--rotate-left", action="store_true",
                        help="Rotate 90 degrees to the left(counterclockwise)")
    parser.add_argument("--rotate-right", action="store_true",
                        help="Rotate 90 degrees to the right(clockwise)")
    parser.add_argument("--disable-zoedepth-batch", action="store_true",
                        help="disable batch processing for low memory GPU")
    parser.add_argument("--keyframe", action="store_true",
                        help="process only keyframe as image")
    parser.add_argument("--keyframe-interval", type=float, default=4.0,
                        help="keyframe minimum interval (sec)")
    parser.add_argument("--vf", type=str, default="",
                        help=("video filter options for ffmpeg."
                              "Note thet the video filter that modify the image size will cause errors."))
    args = parser.parse_args()
    assert not (args.rotate_left and args.rotate_right)
    if args.method == "row_flow" and (args.divergence != 2.5 and args.divergence != 2.0):
        raise ValueError("--method row_flow only supports --divergence 2.5 or 2.0")

    if args.update:
        update_model()
    if args.remove_bg:
        global rembg
        import rembg
        args.bg_session = rembg.new_session(model_name=args.bg_model)
    else:
        args.bg_session = None

    depth_model = load_depth_model(model_type=args.depth_model, gpu=args.gpu)
    if args.method == "row_flow":
        if args.divergence == 2.0:
            model_path = FLOW_D20_MODEL_PATH
        elif args.divergence == 2.5:
            model_path = FLOW_D25_MODEL_PATH
        side_model = load_model(model_path, device_ids=[args.gpu])[0].eval()
        setattr(side_model, "fixed_divergence", args.divergence)
    else:
        side_model = None

    if path.isdir(args.input):
        process_images(args, depth_model, side_model)
    else:
        mime = mimetypes.guess_type(args.input)[0]
        if mime.startswith("video"):
            process_video(args, depth_model, side_model)
        else:
            if mime == "text/plain":
                files = []
                with open(args.input, mode="r", encoding="utf-8") as f:
                    for line in f.readlines():
                        files.append(line.strip())
            else:
                files = [args.input]
            for input_file in files:
                mime = mimetypes.guess_type(input_file)[0]
                if mime.startswith("video"):
                    args.input = input_file
                    process_video(args, depth_model, side_model)
                elif mime.startswith("image"):
                    if path.isdir(args.output) or "." not in path.basename(args.output):
                        os.makedirs(args.output, exist_ok=True)
                        output_filename = path.join(args.output, make_output_filename(input_file))
                    else:
                        output_filename = args.output
                    im, _ = load_image_simple(input_file, color="rgb")
                    output = process_image(im, args, depth_model, side_model)
                    output.save(output_filename)


if __name__ == "__main__":
    main()
