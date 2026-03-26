# tools/timebeat/ — Renesas 8A34002 / Timebeat OTC Tools

Tools for interacting with the Renesas 8A34002 ClockMatrix chip found
on Timebeat OTC and OTC Mini PT hardware (hosts `otcBob1`, `ptBoat`).

Requires `smbus2` (`pip install smbus2`) and I2C access.

| Tool | Purpose |
|------|---------|
| `renesas_init.py` | Initialize Renesas clock tree for peppar-fix |
| `renesas_tdc.py` | Read TDC phase measurements via I2C |
| `tdc_reader.py` | Lower-level TDC reader |
| `eeprom_tool.py` | Dump and restore Renesas EEPROM via I2C |

See `docs/timebeat-otc-research.md` and `docs/timebeat-otc-signal-routing.md`
for background on the ClockMatrix architecture.
