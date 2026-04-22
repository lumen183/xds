KSRC = /lib/modules/$(shell uname -r)/build

.PHONY: all clean

all:
	$(MAKE) M=$(shell pwd) -C $(KSRC) modules

obj-m := stub.o
obj-m += p2p_dev.o

clean:
	rm -rf *.o *.ko *.mod.c *.mod.o *.mod modules.* Module.* .*.ko.cmd .*.mod.o.cmd .*.o.cmd
	rm -rf .*.mod.cmd .tmp_versions/