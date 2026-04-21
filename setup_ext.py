from setuptools import setup, Extension
import subprocess

cflags = subprocess.check_output(["pkg-config", "--cflags", "libavformat", "libavcodec", "libswscale", "libavutil"]).decode().strip().split()
ldflags = subprocess.check_output(["pkg-config", "--libs", "libavformat", "libavcodec", "libswscale", "libavutil"]).decode().strip().split()

omnistream = Extension(
    "omnistream",
    sources=["omnistream.c"],
    extra_compile_args=cflags,
    extra_link_args=ldflags,
)

setup(
    name="omnistream",
    version="1.0.0",
    ext_modules=[omnistream],
)
