# SP16 Emulator: the common build configurations as make targets.
#
# Every target is a thin call to ./sp16, which holds all the logic (`./sp16 help` lists every command and option).
# Run `make` or `make help` for the targets and the variables you can override, for example:
#   make build-record                     build with WAV recording (silent)
#   make bootstrap UPD=~/Downloads/SP16.UPD GPL="gpl-1.zip gpl-2.zip" SAMPLES=~/Samples   everything: build, import, stick, run
#   make import UPD=~/Downloads/SP16.UPD GPL="gpl-1.zip gpl-2.zip"
#   make run NAME=sp16-record
# Use `make -n <target>` to see the exact command without running it.
#
# The recipes are single commands, so the shell options below only matter to GNU Make 3.82 or later (macOS ships 3.81,
# which ignores .SHELLFLAGS); nothing here needs a newer make.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DELETE_ON_ERROR:
MAKEFLAGS += --warn-undefined-variables --no-builtin-rules
.SUFFIXES:
.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------------------------------- variables
# Lines starting with "#: " document a variable for `make help` (a trailing comment would become part of the value).
#: NAME  build name: builds go to build/NAME (variants append -audio, -record, -dev, -dev-uncal); default sp16
NAME ?= sp16
#: AUDIO  `make build`: file (record WAVs, silent), coreaudio (AUDIBLE) or both; empty = silent. `make bootstrap`: empty = coreaudio
AUDIO ?=
#: AUDIO_DEVICE  with coreaudio: output device UID (list them with `make audio-devices`)
AUDIO_DEVICE ?=
#: FIRMWARE  firmware id for run and hwsuite-smoke (default: the library's selected firmware)
FIRMWARE ?=
#: USB_IMAGE  the virtual USB stick image for run and the usb-* targets (default build/stick.img)
USB_IMAGE ?=
#: FILES  usb-add: the sample files (or folders) to copy onto the stick, separated by spaces
FILES ?=
#: DEST  usb-add: copy into this folder under PIONEER DJ SAMPLER instead of Samples, for example Projects
DEST ?=
#: UPD  import: your firmware update file (.UPD)
UPD ?=
#: KEY  import: your key file (or use GPL)
KEY ?=
#: GPL  import: the GPL source archive parts in order, separated by spaces (no spaces inside a path)
GPL ?=
#: VERSION  bootstrap (given on the command line): download this official firmware version (1.60, 1.50, 1.40 or 1.31) instead of UPD
VERSION ?=
#: YES  bootstrap: 1 = you agree to the VERSION download from the official server (no question asked)
YES ?=
#: SAMPLES  bootstrap: sample folders or .zip files for the stick, separated by spaces (no spaces inside a path)
SAMPLES ?=
#: PROJECTS  bootstrap: demo project folders or .zip files for the stick, separated by spaces
PROJECTS ?=
#: PROFILE  hwsuite-smoke: firmware version or profile file (default: the firmware's own version)
PROFILE ?=
#: REBUILD  1 = rebuild an existing build in place (keeps firmware and data)
REBUILD ?=
#: ARGS  extra options appended to the ./sp16 command (for example ARGS="--research")
ARGS ?=
#: PYTHON  runs ./sp16 itself (Python 3.9 or later); default /usr/bin/python3
PYTHON ?= /usr/bin/python3
#: TOOL_PYTHON  runs the build tools (Python 3.12 or later; default: the first one found)
TOOL_PYTHON ?=
#: PREFIX  install: sp16 is linked into $(PREFIX)/bin; default ~/.local
PREFIX ?= $(HOME)/.local
#: CONFIRM  clean-all: must be 1
CONFIRM ?=

SP16EMU := "$(PYTHON)" ./sp16
ifneq ($(strip $(TOOL_PYTHON)),)
export SP16EMU_PYTHON := $(TOOL_PYTHON)
endif
REBUILD_FLAG       = $(if $(strip $(REBUILD)),--rebuild)
AUDIO_FLAG         = $(if $(strip $(AUDIO)),--audio "$(AUDIO)")
AUDIO_DEVICE_FLAG  = $(if $(strip $(AUDIO_DEVICE)),--audio-device "$(AUDIO_DEVICE)")
FIRMWARE_FLAG      = $(if $(strip $(FIRMWARE)),--firmware "$(FIRMWARE)")
USB_IMAGE_FLAG     = $(if $(strip $(USB_IMAGE)),--usb-image "$(USB_IMAGE)")
USB_IMAGE_ARG      = $(if $(strip $(USB_IMAGE)),--image "$(USB_IMAGE)")
PROFILE_FLAG       = $(if $(strip $(PROFILE)),--profile "$(PROFILE)")
DEST_FLAG          = $(if $(strip $(DEST)),--dest "$(DEST)")
# VERSION counts only when it is given on the command line, so a VERSION in your environment never starts a download.
BOOT_VERSION       = $(if $(findstring command line,$(origin VERSION)),$(strip $(VERSION)))
BOOT_FIRMWARE      = $(if $(BOOT_VERSION),--download-firmware "$(BOOT_VERSION)",$(if $(strip $(UPD)),--update "$(UPD)"))
BOOT_KEY           = $(if $(strip $(KEY)),--key-file "$(KEY)")$(if $(strip $(GPL)), --gpl-archive $(GPL))
BOOT_YES           = $(if $(strip $(YES)),--yes)
# Maintenance targets use --build NAME only when you set NAME yourself; otherwise ./sp16 picks the default build.
BUILD_FLAG         = $(if $(or $(findstring command line,$(origin NAME)),$(findstring environment,$(origin NAME))),--build "$(NAME)")

# Fail early, before anything runs, when a required variable is missing.
ifneq ($(filter import,$(MAKECMDGOALS)),)
ifeq ($(strip $(UPD)),)
$(error make import needs UPD=<your firmware update file>, plus KEY=<key file> or GPL="<part 1> <part 2>")
endif
ifeq ($(strip $(KEY))$(strip $(GPL)),)
$(error make import needs KEY=<your key file> or GPL="<GPL archive part 1> <part 2>" besides UPD)
endif
endif
ifneq ($(filter bootstrap,$(MAKECMDGOALS)),)
ifneq ($(strip $(UPD)),)
ifneq ($(BOOT_VERSION),)
$(error make bootstrap takes UPD=<your firmware update file> or VERSION=<1.60|1.50|1.40|1.31> (a download), not both)
endif
endif
ifneq ($(strip $(UPD))$(BOOT_VERSION),)
ifeq ($(strip $(KEY))$(strip $(GPL)),)
$(error make bootstrap needs KEY=<your key file> or GPL="<GPL archive part 1> <part 2>" besides UPD or VERSION)
endif
endif
endif
ifneq ($(filter usb-add,$(MAKECMDGOALS)),)
ifeq ($(strip $(FILES)),)
$(error make usb-add needs FILES="<sample files, separated by spaces>", for example make usb-add FILES="~/samples/*.wav")
endif
endif
ifneq ($(filter clean-all,$(MAKECMDGOALS)),)
ifneq ($(CONFIRM),1)
$(error make clean-all deletes everything in build/, including the imported firmware and data images; run: make clean-all CONFIRM=1)
endif
endif

.PHONY: help doctor bootstrap build build-audio build-record build-dev build-control run run-audio run-dev audio-devices \
	usb-create usb-add usb-ls usb-mount usb-unmount import list diagnostics smoke hwsuite-smoke test coverage clean clean-all \
	install uninstall

# ------------------------------------------------------------------------------------------------------ targets
help: ## Show the targets and variables
	@echo "SP16 Emulator. Usage: make <target> [VARIABLE=value ...]"
	@echo
	@echo "Targets:"
	@awk 'match($$0, /^[a-z][a-z-]*:.*## /) { split($$0, p, ":"); text = $$0; sub(/^[^#]*## /, "", text); printf "  %-15s %s\n", p[1], text }' $(MAKEFILE_LIST)
	@echo
	@echo "Variables (override on the command line, for example make build NAME=mine):"
	@awk '/^#: [A-Z_]+  / { name = $$2; text = $$0; sub(/^#: [A-Z_]+  /, "", text); printf "  %-15s %s\n", name, text }' $(MAKEFILE_LIST)
	@echo
	@echo "Everything these targets do is also available as ./sp16 <command>; see ./sp16 help."

doctor: ## Check this Mac for everything a build needs
	$(SP16EMU) doctor

bootstrap: ## Check, build with audio, import firmware (UPD or VERSION, plus KEY or GPL), fill the stick (SAMPLES, PROJECTS), run
	$(SP16EMU) bootstrap --name "$(NAME)" --audio "$(or $(strip $(AUDIO)),coreaudio)" $(AUDIO_DEVICE_FLAG) $(REBUILD_FLAG) $(BOOT_FIRMWARE) $(BOOT_KEY) $(BOOT_YES) $(foreach s,$(SAMPLES),--samples "$(s)") $(foreach p,$(PROJECTS),--projects "$(p)") $(ARGS)

build: ## Build build/NAME (silent unless AUDIO is set; REBUILD=1 rebuilds in place)
	$(SP16EMU) build --name "$(NAME)" $(AUDIO_FLAG) $(AUDIO_DEVICE_FLAG) $(REBUILD_FLAG) $(ARGS)

build-audio: ## Build build/NAME-audio that plays through the Mac's output (AUDIBLE)
	$(SP16EMU) build --name "$(NAME)-audio" --audio coreaudio $(AUDIO_DEVICE_FLAG) $(REBUILD_FLAG) $(ARGS)

build-record: ## Build build/NAME-record that records each audio stream to WAV files (silent)
	$(SP16EMU) build --name "$(NAME)-record" --audio file $(REBUILD_FLAG) $(ARGS)

build-dev: ## Build build/NAME-dev for modding and tests: research socket, WAV recording, bundle suffix dev
	$(SP16EMU) build --name "$(NAME)-dev" --research --audio file --bundle-suffix dev $(REBUILD_FLAG) $(ARGS)

build-control: ## Build build/NAME-dev-uncal, the uncalibrated control build for the hardware suite
	$(SP16EMU) build --name "$(NAME)-dev-uncal" --research --audio file --uncalibrated --bundle-suffix dev-uncal $(REBUILD_FLAG) $(ARGS)

run: ## Launch build/NAME (FIRMWARE and USB_IMAGE are optional; build/stick.img is attached when it exists)
	$(SP16EMU) run --name "$(NAME)" $(FIRMWARE_FLAG) $(USB_IMAGE_FLAG) $(ARGS)

run-audio: ## Launch build/NAME-audio
	$(SP16EMU) run --name "$(NAME)-audio" $(FIRMWARE_FLAG) $(USB_IMAGE_FLAG) $(ARGS)

run-dev: ## Launch build/NAME-dev
	$(SP16EMU) run --name "$(NAME)-dev" $(FIRMWARE_FLAG) $(USB_IMAGE_FLAG) $(ARGS)

audio-devices: ## List the Mac's audio output devices and their UIDs (for AUDIO_DEVICE)
	$(SP16EMU) audio devices

usb-create: ## Create and format the virtual USB stick (build/stick.img, or USB_IMAGE=...) without booting
	$(SP16EMU) usb create $(USB_IMAGE_ARG) $(ARGS)

usb-add: ## Copy files onto the stick (FILES="a.wav b.wav"; DEST=Projects for project files), creating it if needed
	$(SP16EMU) usb add $(FILES) $(USB_IMAGE_ARG) $(DEST_FLAG) $(ARGS)

usb-ls: ## List what is on the stick
	$(SP16EMU) usb ls $(USB_IMAGE_ARG) $(ARGS)

usb-mount: ## Mount the stick on the Mac and print the mount point (never while the emulator runs)
	$(SP16EMU) usb mount $(USB_IMAGE_ARG) $(ARGS)

usb-unmount: ## Release the stick again
	$(SP16EMU) usb unmount $(USB_IMAGE_ARG) $(ARGS)

import: ## Import your firmware into the shared library: UPD=file.UPD and KEY=key or GPL="part1 part2"
	$(SP16EMU) firmware import $(BUILD_FLAG) --update "$(UPD)" $(if $(strip $(KEY)),--key-file "$(KEY)") $(if $(strip $(GPL)),--gpl-archive $(GPL)) $(ARGS)

list: ## List the builds and the imported firmware
	$(SP16EMU) builds
	$(SP16EMU) firmware list $(BUILD_FLAG)

diagnostics: ## Collect a redacted diagnostics zip for a bug report
	$(SP16EMU) diagnostics $(BUILD_FLAG) $(ARGS)

smoke: ## Run the end-to-end smoke test on build/NAME-dev and record a sped-up screencast (see make build-dev)
	$(SP16EMU) smoke --build "$(NAME)-dev" $(FIRMWARE_FLAG) $(ARGS)

hwsuite-smoke: ## Run the hardware conformance suite's smoke tier on build/NAME-dev (see make build-dev, build-control)
	$(SP16EMU) hwsuite --smoke --build "$(NAME)-dev" $(PROFILE_FLAG) $(FIRMWARE_FLAG) $(ARGS)

test: ## Run the firmware-free unit tests
	$(SP16EMU) test

coverage: ## Run the unit tests with line and branch coverage (needs coverage.py); writes build/coverage/coverage.xml
	$(SP16EMU) test --coverage

clean: ## Delete build outputs; keeps the firmware library and data images
	$(SP16EMU) clean --all

clean-all: ## Delete everything in build/ including firmware (needs CONFIRM=1)
	$(SP16EMU) clean --everything --yes

install: ## Link sp16 into $(PREFIX)/bin (no shell files are touched)
	mkdir -p "$(PREFIX)/bin"
	if [ -e "$(PREFIX)/bin/sp16" ] && [ ! -L "$(PREFIX)/bin/sp16" ]; then echo "$(PREFIX)/bin/sp16 exists and is not a link; move it away first" >&2; exit 1; fi
	ln -sfn "$(CURDIR)/sp16" "$(PREFIX)/bin/sp16"
	echo "Linked $(PREFIX)/bin/sp16. If 'sp16' is not found, add $(PREFIX)/bin to your PATH."

uninstall: ## Remove the link that `make install` made
	if [ -L "$(PREFIX)/bin/sp16" ] && [ "$$(readlink "$(PREFIX)/bin/sp16")" = "$(CURDIR)/sp16" ]; then rm "$(PREFIX)/bin/sp16" && echo "Removed $(PREFIX)/bin/sp16"; else echo "Nothing to remove: $(PREFIX)/bin/sp16 is not a link to this checkout"; fi
