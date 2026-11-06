# Scanout color-order investigation

## Conclusion

The inspected driver programs an RGBA8 CB target (`0x00008028`). Before this fix, all five checked-in native runtime variants used `0x8000000000000000` for VideoOut scanout. Mesa's PS5 WSI source names that value `B8G8R8A8_SDR`. A separate public OpenProspero/AGC implementation uses `0x8000000022000000` with 32-bit pixels encoded so their little-endian memory bytes are R,G,B,A. This supports replacing the runtime VideoOut value with `0x8000000022000000`; it does not support changing CB, EGL, or texture formats.

The evidence comes from public implementations and a public format mapping, not official Sony format documentation. The correction is not yet hardware-verified.

## Repository path

- `patches/ps5-draw-batching/ps5_screen.c` includes Mesa's `amdgfxregs.h`, `ac_formats.h`, and `gfx10_format_table.h`. `ps5_color_target_info()` (lines 1221–1254) asks Mesa for `ac_get_cb_format(GFX10, format)`, `ac_get_cb_number_type(format)`, and `ac_translate_colorswap(GFX10, format, false)`, then packs those values at shifts 2, 8, and 11. Its fallback assigns `0x00008028` to `PIPE_FORMAT_R8G8B8A8_UNORM`; sRGB has a separate `0x00008628` value.
- `ps5_fragment_exports_for_framebuffer()` uses the resulting info with `G_028C70_FORMAT_GFX6`, `G_028C70_COMP_SWAP`, and `G_028C70_NUMBER_TYPE` to choose shader export formats. This is CB register state, not the VideoOut attribute.
- `patches/ps5-draw-batching/platform/ps5_agc_runtime_backend.c` initializes all MRT info words to `0x00008028`. `ps5_agc_linear_color_bytes()` maps that exact word to 4 bytes, described as `RGBA8_UNORM`; the switch is used when calculating a linear color target extent. It does not convert the CB word into a VideoOut format.
- The five runtime variants in this repository now define `VIDEO_OUT_PIXEL_FORMAT` as `0x8000000022000000` and pass it to `set_attribute2()` at both the packaged and diagnostic paths. Their target validation uses the private `NATIVE_COLOR_FORMAT_RGBA8_UNORM == 1` enum and swizzle code 27; neither is a mapping to the VideoOut enum.

Candidate build scripts select these variants as follows: `tools/build-production-profile-candidate.py` sources `patches/ps5-production-profile`; `tools/build-default-cache-candidate.py` selects `patches/ps5-default-cache`; `tools/build-native-profile-candidate.py` selects `patches/ps5-native-profile`; and `tools/build-poll-spin-candidate.py` selects `patches/ps5-poll-spin`. The normal draw-batching runtime comes from `patches/ps5-draw-batching/platform/`. All five copies now use the corrected scanout attribute.

## Public AMD/Mesa evidence

AMD/Mesa's GFX10 register metadata defines `CB_COLOR0_INFO.FORMAT` in bits 6:2 and `COLOR_8_8_8_8` as value 10. Thus `10 << 2 == 0x28`; the low CB format bits in `0x00008028` match that definition. The other set high bit (`0x8000`) is outside the format field. Primary metadata permalink: [Mesa GFX10 register metadata](https://chromium.googlesource.com/chromiumos/third_party/mesa/+/refs/heads/stabilize-13982.70.B-chromeos-amd/src/amd/registers/gfx10.json).

Mesa's `ac_get_cb_format()` maps four uniform 8-bit channels to `V_028C70_COLOR_8_8_8_8`. `ac_translate_colorswap()` selects `V_028C70_SWAP_STD` for the ordinary X/Y/Z/W component sequence. The driver’s pack operation puts the latter in bits starting at 11. Primary source permalink: [Mesa AMD format translation (`ac_formats.c`, pinned commit)](https://chromium.googlesource.com/external/gitlab.freedesktop.org/mesa/mesa/+/418c4cfa6708a0e0b1175e72fb8fd27d3ca1615a/src/amd/common/ac_formats.c).

Generated register headers are not the best source for the numeric encoding; Mesa generates them from register metadata. Do not confuse the GFX10 code 10 with older Radeon `COLOR_8_8_8_8` encodings. The linked Mesa metadata explicitly identifies the GFX10 field and generation.

## Additional public PS5/VideoOut evidence

The pinned [PS5_Mesa WSI implementation](https://github.com/mihawk-99/PS5_Mesa/blob/f5610fe27090acd2509e5ce807140676f0b04473/src/vulkan/wsi/wsi_common_videoout.c) defines `0x8000000000000000` as `PS5_VIDEO_OUT_PIXEL_FORMAT_B8G8R8A8_SDR` and advertises `VK_FORMAT_B8G8R8A8_UNORM`/SRGB. This identifies the previous runtime's VideoOut format as B8G8R8A8 in that implementation's mapping.

The pinned [OpenProspero/AGC VideoOut implementation](https://github.com/OpenProspero/AGC/blob/7613a009f0108c80ee7548694aaf17aa33615dbc/src/openagc_ps5_videoout.c) registers buffers with `0x8000000022000000`. Its `pixel()` returns `0xff000000 | (B << 16) | (G << 8) | R`; on the little-endian PS5/x86 target, writing that word yields R,G,B,A bytes. It therefore demonstrates the `0x...22000000` attribute in use with RGBA byte-order pixels.

The [OpenGnm VideoOut helper reference](https://ps4-opengnm.github.io/opengnm/reference/helpers/) names the corresponding legacy PS4 value `GNM_VIDEO_OUT_PIXEL_FORMAT_A8B8G8R8_SRGB` (`0x80002200`); the [pinned AnyPS5 translation](https://github.com/boykopovar/AnyPS5/blob/6a120ae1b7f051c418188cd59ccd12c974b0019f/core/libs/prx/libSceVideoOut/src/Buffer.cpp) maps that legacy value to PS5 `0x8000000022000000`. These corroborate the encoding relationship, but are not official Sony documentation.

## Evidence boundary

The GFX10 CB interpretation is established by AMD/Mesa register metadata and format translation. The public PS5 implementations and the legacy mapping support the VideoOut correction to `0x8000000022000000`. Console validation is still required.
