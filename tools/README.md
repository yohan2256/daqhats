# Headless MCC172 tools

Installed by the root install.sh:

- daqhats_read_eeproms: read HAT EEPROM information (run with sudo).
- daqhats_list_boards: confirm detected HATs.
- daqhats_version: report the native library version.
- mcc172_firmware_update: firmware maintenance when required; MCC_172.fw is retained.

Desktop control panels and tools for unrelated boards were removed. Native
library modules remain together because the shared ABI and Python exports
reference them. Original upstream code and history: https://github.com/mccdaq/daqhats
