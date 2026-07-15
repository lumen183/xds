#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/ioctl.h>
#include <time.h>
#include <errno.h>

#include <linux/fiemap.h>
#include <linux/fs.h>

#include "p2p_dev_uapi.h"
#include "file_p2p_api.h"

long long timespec_diff_ms(const struct timespec *start, const struct timespec *end)
{
    return (end->tv_sec - start->tv_sec) * 1000LL + (end->tv_nsec - start->tv_nsec) / 1000000LL;
}

void close_p2p_fd(int dev_fd)
{
    close(dev_fd);
}

int drain_read(int dev_fd)
{
    int err;

    err = ioctl(dev_fd, IOCTL_DRAIN_READ, 0);
    if (err < 0)
        return -errno;
    return 0;
}

int new_p2p_fd(void)
{
    int dev_fd;
    int err;

    dev_fd = open("/dev/p2p_device", O_RDWR);
    if (dev_fd < 0) {
        err = -errno;
        fprintf(stderr, "open /dev/p2p_device failed, errno: %d\n", err);
        return err;
    }

    return dev_fd;
}

int read_file(int dev_fd, struct read_parameter *param)
{
    struct read_desc *read;
    struct fiemap *exts;
    struct stat file_stat;
    unsigned long max_size;
    unsigned long total_size;
    unsigned int max_num;
    unsigned int ext_num;
    const char *name;
    unsigned int i;
    int file_fd = -1;
    int bdev_fd = -1;
    int err = 0;

    name = param->file_name;
    file_fd = open(name, O_RDONLY);
    if (file_fd < 0) {
        err = -errno;
        fprintf(stderr, "open %s failed, errno: %d\n", name, err);
        goto close_fds_out;
    }

    err = fstat(file_fd, &file_stat);
    if (err < 0) {
        err = -errno;
        fprintf(stderr, "fstat %s failed, errno: %d\n", name, err);
        goto close_fds_out;
    }

    bdev_fd = open(param->bdev_name, O_RDONLY);
    if (bdev_fd < 0) {
        err = -errno;
        fprintf(stderr, "open %s failed, errno: %d\n", param->bdev_name, err);
        goto close_fds_out;
    }

    max_size = param->size;
    if ((file_stat.st_mode & S_IFMT) == S_IFBLK)
        max_num = 1;
    else
        max_num = max_size >> 12;

    exts = malloc(sizeof(*exts) + max_num * sizeof(exts->fm_extents[0]));
    if (exts == NULL) {
        err = -ENOMEM;
        fprintf(stderr, "malloc fiemap failed, errno: %d\n", err);
        goto close_fds_out;
    }

    if ((file_stat.st_mode & S_IFMT) == S_IFBLK) {
        exts->fm_extents[0].fe_physical = param->bdev_offset;
        exts->fm_extents[0].fe_length = max_size;
        ext_num = 1;
        total_size = max_size;
        goto to_read;
    }

    exts->fm_start = param->bdev_offset;
    exts->fm_length = max_size;
    exts->fm_flags = 0;
    exts->fm_extent_count = max_num;

    err = ioctl(file_fd, FS_IOC_FIEMAP, exts);
    if (err) {
        err = -errno;
        fprintf(stderr, "ioctl FS_IOC_FIEMAP failed, errno: %d\n", err);
        goto free_ext_out;
    }

    total_size = 0;
    ext_num = exts->fm_mapped_extents;
    exts->fm_extents[0].fe_physical += param->bdev_offset % 4096;
    exts->fm_extents[0].fe_length -= param->bdev_offset % 4096;
    
    for (i = 0; i < ext_num; i++) {
        if (total_size + exts->fm_extents[i].fe_length > param->size) {
            exts->fm_extents[i].fe_length = param->size - total_size;
            total_size += exts->fm_extents[i].fe_length;
            ext_num = i + 1;
            break;
        }
        total_size += exts->fm_extents[i].fe_length;
    }

    if (total_size > param->size) {
        err = -EINVAL;
        fprintf(stderr, "total_size %lu > param->size %u\n", total_size, param->size);
        goto free_ext_out;
    }

to_read:
    read = malloc(sizeof(*read) + ext_num * sizeof(read->extents[0]));
    if (read == NULL) {
        err = -ENOMEM;
        fprintf(stderr, "malloc read_desc failed, errno: %d\n", err);
        goto free_ext_out;
    }

    read->desc.hostpid = getpid();
    read->desc.devid = param->devid;
    read->desc.vfid = param->vfid;
    read->desc.addr = param->addr;
    read->desc.size = param->size;
    read->bdev_fd = bdev_fd;
    read->nsid = 1;
    read->file_fd = file_fd;
    read->ext_num = ext_num;
    memcpy(read->extents, exts->fm_extents, ext_num * sizeof(read->extents[0]));

    err = ioctl(dev_fd, IOCTL_READ_FILE, read);
    if (err < 0) {
        err = -errno;
        fprintf(stderr,
                "ioctl IOCTL_READ_FILE failed: errno=%d (%s) file=%s bdev=%s "
                "offset=%lu addr=0x%lx size=%u devid=%u vfid=%u extents=%u\n",
                -err, strerror(-err), param->file_name, param->bdev_name,
                param->bdev_offset, param->addr, param->size,
                param->devid, param->vfid, ext_num);
        goto free_read_out;
    }

free_read_out:
    free(read);
free_ext_out:
    free(exts);
close_fds_out:
    if (file_fd >= 0)
        close(file_fd);
    if (bdev_fd >= 0)
        close(bdev_fd);

    return err;
}

int read_file_batch(int dev_fd, struct read_parameter *param, int param_num)
{
    struct read_desc_ba *read;
    struct fiemap *exts;
    unsigned long max_size;
    unsigned long total_size;
    unsigned int max_num;
    unsigned int ext_num;
    const char *name;
    unsigned int i;
    int j;
    int file_fd = -1;
    int bdev_fd = -1;
    int err = 0;

    name = param[0].file_name;
    file_fd = open(name, O_RDONLY);
    if (file_fd < 0) {
        err = -errno;
        fprintf(stderr, "open %s failed, errno: %d\n", name, err);
        goto close_fds_out;
    }

    bdev_fd = open(param[0].bdev_name, O_RDONLY);
    if (bdev_fd < 0) {
        err = -errno;
        fprintf(stderr, "open %s failed, errno: %d\n", param[0].bdev_name, err);
        goto close_fds_out;
    }

    max_size = param[0].size * param_num;
    max_num = max_size >> 12;

    exts = malloc(sizeof(*exts) + max_num * sizeof(exts->fm_extents[0]));
    if (exts == NULL) {
        err = -ENOMEM;
        fprintf(stderr, "malloc fiemap failed, errno: %d\n", err);
        goto close_fds_out;
    }

    exts->fm_start = param[0].bdev_offset;
    exts->fm_length = max_size;
    exts->fm_flags = 0;
    exts->fm_extent_count = max_num;

    err = ioctl(file_fd, FS_IOC_FIEMAP, exts);
    if (err) {
        err = -errno;
        fprintf(stderr, "ioctl FS_IOC_FIEMAP failed, errno: %d\n", err);
        goto free_ext_out;
    }

    total_size = 0;
    ext_num = exts->fm_mapped_extents;
    exts->fm_extents[0].fe_physical += param[0].bdev_offset % 4096;
    exts->fm_extents[0].fe_length -= param[0].bdev_offset % 4096;
    
    for (i = 0; i < ext_num; i++) {
        if (total_size + exts->fm_extents[i].fe_length > max_size) {
            exts->fm_extents[i].fe_length = max_size - total_size;
            total_size += exts->fm_extents[i].fe_length;
            ext_num = i + 1;
            break;
        }
        total_size += exts->fm_extents[i].fe_length;
    }

    if (total_size > max_size) {
        err = -EINVAL;
        fprintf(stderr, "total_size %ld > max_size %ld\n", total_size, max_size);
        goto free_ext_out;
    }

    read = malloc(sizeof(*read) + ext_num * sizeof(read->extents[0]));
    if (read == NULL) {
        err = -ENOMEM;
        fprintf(stderr, "malloc read_desc failed, errno: %d\n", err);
        goto free_ext_out;
    }

    read->desc.hostpid = getpid();
    read->desc.devid = param[0].devid;
    read->desc.vfid = param[0].vfid;
    read->desc.count = param_num;
    read->bdev_fd = bdev_fd;
    read->nsid = 1;
    read->file_fd = file_fd;
    read->ext_num = ext_num;
    memcpy(read->extents, exts->fm_extents, ext_num * sizeof(read->extents[0]));

    read->desc.addr = malloc(param_num * sizeof(unsigned long));
    read->desc.size = malloc(param_num * sizeof(unsigned long));
    if (read->desc.addr == NULL || read->desc.size == NULL) {
        err = -ENOMEM;
        fprintf(stderr, "malloc read_desc desc failed, errno: %d\n", err);
        goto free_read_out;
    }
    for (j = 0; j < param_num; j++) {
        read->desc.addr[j] = param[j].addr;
        read->desc.size[j] = param[j].size;
    }

    err = ioctl(dev_fd, IOCTL_READ_FILE_BATCH, read);
    if (err < 0) {
        err = -errno;
        fprintf(stderr,
                "ioctl IOCTL_READ_FILE_BATCH failed: errno=%d (%s) file=%s bdev=%s "
                "requests=%d extents=%u\n",
                -err, strerror(-err), param[0].file_name, param[0].bdev_name,
                param_num, ext_num);
        goto free_read_out;
    }

free_read_out:
    if (read->desc.addr != NULL)
        free(read->desc.addr);
    if (read->desc.size != NULL)
        free(read->desc.size);
    free(read);
free_ext_out:
    free(exts);
close_fds_out:
    if (file_fd >= 0)
        close(file_fd);
    if (bdev_fd >= 0)
        close(bdev_fd);

    return err;
}
