libfile_p2p.so: file_p2p_api.c file_p2p_api.h test.mk
			gcc -fPIC -shared -o $@ file_p2p_api.c -Wall
