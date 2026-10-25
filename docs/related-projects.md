# Related DJS-1000 emulation projects

Independent projects that run the DJS-1000 application. Read for ideas and cross-checks; no code is shared with this
repository, and licences differ, so do not copy code between them.

| Project | Licence | How it runs | Platforms | Notable features |
|---|---|---|---|---|
| [commit-collective/djs1000-emu](https://github.com/commit-collective/djs1000-emu) | GPL-2.0-or-later | the app under `qemu-arm` user mode in a chroot, one preload shim, a pygame "virtual deck" | Linux, Windows (WSL2); macOS only through a Linux VM | PRO DJ LINK, audio input, headphone output, `autoexec.bin` mod flow, session regression tool |
| this project | Unlicense | a full Debian guest under QEMU with Hypervisor.framework; the app runs under `qemu-arm-static` inside it | macOS (Apple silicon, Intel) | native panel and audio, firmware importer and library, profiles, hardware suite |

The two panel tables (button bits, encoders, lamps, tempo slider, touch words) were worked out independently and
agree. Where this project lacks a feature that the other has, the plan is to rebuild it from the documented behaviour
and our own measurements, not from its source.
