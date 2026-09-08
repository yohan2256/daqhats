CC = gcc
CFLAGS = -I../include -I../lib -Wall -O2
LDLIBS = -ldaqhats
INSTALL_DIR = /usr/local/bin
BINARIES = mcc172_firmware_update daqhats_list_boards
SCRIPTS = daqhats_read_eeproms daqhats_version

.PHONY: all install uninstall clean
all: $(BINARIES)

$(BINARIES): %: %.c ../include/daqhats.h ../include/mcc172.h ../lib/mcc172_update.h
	$(CC) $(CFLAGS) $(LDFLAGS) $< -o $@ $(LDLIBS)

install: all
	install -d $(INSTALL_DIR)
	install -m 755 $(BINARIES) $(SCRIPTS) $(INSTALL_DIR)

uninstall:
	$(RM) $(addprefix $(INSTALL_DIR)/,$(BINARIES) $(SCRIPTS))

clean:
	$(RM) $(BINARIES) *.o
