# Intel IGC PPS Fix for Kernel 6.12 (RPi5 TimeHAT)

Patched igc driver based on upstream Linux 6.12 with PPS input/output fixes for the TimeHAT I226 NIC.

## PPS Fixes Applied

1. **EXTTS (PPS Input):** Bypasses strict dual-edge flag requirement, forces rising-edge only
2. **PEROUT (PPS Output):** Fixes 1PPS output so `testptp -p 1000000000` produces proper pulses instead of 1Hz square wave
3. **Pin Tracking:** Adds per-channel EXTTS pin and flags tracking

## Install

```bash
sudo apt install -y dkms raspberrypi-kernel-headers

sudo dkms remove igc -v 6.12.0-ppsfix.1 2>/dev/null
sudo dkms add .
sudo dkms build --force igc -v 6.12.0-ppsfix.1
sudo dkms install --force igc -v 6.12.0-ppsfix.1

# Replace stock module
sudo cp /lib/modules/$(uname -r)/kernel/drivers/net/ethernet/intel/igc/igc.ko.xz \
        /lib/modules/$(uname -r)/kernel/drivers/net/ethernet/intel/igc/igc.ko.xz.bak
sudo cp /lib/modules/$(uname -r)/updates/dkms/igc.ko.xz \
        /lib/modules/$(uname -r)/kernel/drivers/net/ethernet/intel/igc/igc.ko.xz
sudo depmod -a
sudo update-initramfs -u -k $(uname -r)
sudo reboot
```

## Test

```bash
sudo testptp -d /dev/ptp0 -L0,2          # pin 0 as perout
sudo testptp -d /dev/ptp0 -p 1000000000  # 1PPS output
sudo testptp -d /dev/ptp0 -L1,1          # pin 1 as extts
sudo testptp -d /dev/ptp0 -e 5           # read 5 timestamps
```
