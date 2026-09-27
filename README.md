# Firefox for Termux:X11 with MediaCodec zero-copy decoding

Termux package recipes and patches for Firefox hardware video decoding through
Android MediaCodec. Based on [termux/termux-packages](https://github.com/termux/termux-packages),
with the upstream build infrastructure and history retained.

## Packages

- [Firefox 155.0.1-3](x11-packages/firefox): imports decoder NV12 dma-bufs directly
  into hardware WebRender, without an intermediate RGBA allocation or GPU blit.
- [FFmpeg 8.1.2-6](packages/ffmpeg): adds the MediaCodec `output_color_format`
  option required by this Firefox build.
- [Playback test](x11-packages/firefox/tests/playback.py): real playback, seeking,
  canvas readback, decoder component identity and direct-import logging.

Installable aarch64 packages and SHA-256 checksums are available under
[Releases](https://github.com/skmdx/firefox-termux/releases). Install both packages
as a pair, after exiting Firefox.

## Build and runtime

Use the [Termux package build environment](https://github.com/termux/termux-packages/wiki/Building-packages).
Build FFmpeg before Firefox. This repository includes both recipes and their patches.

Direct GPU output requires hardware WebRender and EGL dma-buf import support.
The preferences `media.ffmpeg.mediacodec.enabled` and
`media.ffmpeg.mediacodec.surface.enabled` default to true in this build.
Without hardware WebRender, MediaCodec uses CPU output.

Tested on Quest 3, Android 14 / API 34, Termux:X11, Mesa 26.2.2 Zink/Turnip
(Adreno 740): H.264, HEVC, VP9 and AV1 at 1080p60, non-aligned image widths,
90-second playback and repeated seeking. Surface output is 8-bit NV12,
including HEVC Main10 input; HDR and preservation of 10-bit output are not validated.
These results do not establish support on other devices or GPU drivers.

## License

See [LICENSE.md](LICENSE.md) for the packaging repository license.
Firefox, FFmpeg and other packaged software retain their respective licenses;
the recipes identify the source releases and licenses.
