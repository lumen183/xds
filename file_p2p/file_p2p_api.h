#ifndef __FILE_P2P_API_H__
#define __FILE_P2P_API_H__

struct read_parameter {
    const char *file_name;
    const char *bdev_name;
    unsigned long bdev_offset;
    unsigned short devid;
    unsigned short vfid;
    unsigned int size;
    unsigned long addr;
};

int new_p2p_fd(void);
int close_p2p_fd(int dev_fd);
int read_file(int dev_fd, struct read_parameter *param);
int read_file_batch(int dev_fd, struct read_parameter *params, int param_num);
int drain_read(int dev_fd);

#endif