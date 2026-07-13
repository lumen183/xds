ifneq ($(KERNELRELEASE),)

obj-m += stub.o
obj-m += p2p_dev.o

else

KDIR ?= /lib/modules/$(shell uname -r)/build
MODULE_DIR := $(CURDIR)

.PHONY: all modules clean

all modules:
	$(MAKE) -C $(KDIR) M=$(MODULE_DIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(MODULE_DIR) clean

endif
