#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/init.h>
#include <linux/kdev_t.h>
#include <linux/fs.h>
#include <linux/cdev.h>
#include <linux/device.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/ioctl.h>
#include <linux/vmalloc.h>
#include <linux/ktime.h>
#include <linux/idr.h>
#include <linux/pid.h>
#include <linux/errno.h>
#include <linux/atomic.h>
#include <linux/completion.h>
#include <linux/spinlock.h>
#include <linux/list.h>

#include <linux/nvme.h>
#include <linux/blk_types.h>
#include <linux/file.h>
#include <linux/blkdev.h>
#include <linux/blk-mq.h>
#include <linux/version.h>

#include <trace/events/block.h>

#include "p2p_dev_uapi.h"
#include "p2p_mem_query.h"

#define HW_LIMIT_SIZE (128U << 10)

u64 g_time = 0;
u64 g_count = 0;
u64 g_size = 0;

struct p2p_io_context {
    struct block_device *bdev;
    unsigned int nsid;
    unsigned int pa_num;
    unsigned int pa_idx;
    unsigned int pa_offset;
    u64 *pa_list;
    unsigned int pa_size;
    unsigned int data_size;
    struct completion io_done;
    atomic_t io_ref;
    int io_err;
    int issue_err;
    struct list_head io_list;
    struct nvme_command *cmd_list;
    int cmd_id;
    u64 start_time;
    u64 end_time;
    int count;
};

struct p2p_batch {
    unsigned int batch_id;
    struct list_head io_list;
    unsigned int io_cnt;
    spinlock_t io_lock;
};

#define RQF_NVME_PT ((__force req_flags_t)(1 << 31))

#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 6, 0)
/*
 * nvme_alloc_request() was removed from the upstream NVMe host driver.
 * Linux 6.6 allocates passthrough requests directly from blk-mq and then
 * initializes the NVMe private request state with nvme_init_request().
 * The prototype lives in the NVMe driver's private header, so keep the
 * declaration here for an external module.
 */
void nvme_init_request(struct request *req, struct nvme_command *cmd);
#else
struct request *nvme_alloc_request(struct request_queue *q, struct nvme_command *cmd, blk_mq_req_flags_t flags, int qid);
#endif

dev_t dev = 0;
static struct class *dev_class;
static struct cdev p2p_cdev;

static struct tracepoint *tp_nvme_setup_cmd;

unsigned long tp_nvme_setup_cmd_addr;
module_param(tp_nvme_setup_cmd_addr, ulong, 0400);

/*
 * SVM lookup diagnostics.  These values are exported read-only through
 * /sys/module/p2p_dev/parameters/ so a failed lookup can still be inspected
 * on systems where kernel printk output is unavailable or filtered.
 */
static char *diag_build = "svm-probe-v1";
static unsigned int diag_single_hits;
static unsigned int diag_batch_hits;
static int diag_last_path;
static int diag_desc_hostpid;
static int diag_current_pid;
static int diag_current_tgid;
static int diag_resolved_hostpid;
static int diag_devid;
static int diag_vfid;
static int diag_page_ret = -999;
static int diag_probe0_ret = -999;

module_param(diag_build, charp, 0444);
module_param(diag_single_hits, uint, 0444);
module_param(diag_batch_hits, uint, 0444);
module_param(diag_last_path, int, 0444);
module_param(diag_desc_hostpid, int, 0444);
module_param(diag_current_pid, int, 0444);
module_param(diag_current_tgid, int, 0444);
module_param(diag_resolved_hostpid, int, 0444);
module_param(diag_devid, int, 0444);
module_param(diag_vfid, int, 0444);
module_param(diag_page_ret, int, 0444);
module_param(diag_probe0_ret, int, 0444);

static DEFINE_SPINLOCK(batch_lock);
static DEFINE_IDR(batch_tree);

static int p2p_open(struct inode *inode, struct file *file);
static int p2p_release(struct inode *inode, struct file *file);
static long p2p_ioctl(struct file *file, unsigned int cmd, unsigned long arg);
static int p2p_drain_read(struct p2p_batch *batch);

static struct file_operations fops = {
    .owner = THIS_MODULE,
    .open = p2p_open,
    .release = p2p_release,
    .unlocked_ioctl = p2p_ioctl,
};

static int p2p_open(struct inode *inode, struct file *file)
{
    struct p2p_batch *batch;
    int err;

    batch = kzalloc(sizeof(*batch), GFP_KERNEL);
    if (!batch) {
        return -ENOMEM;
    }

    spin_lock_init(&batch->io_lock);
    INIT_LIST_HEAD(&batch->io_list);
    batch->io_cnt = 0;

    idr_preload(GFP_KERNEL);
    spin_lock(&batch_lock);
    err = idr_alloc(&batch_tree, batch, 1, 0, GFP_ATOMIC);
    spin_unlock(&batch_lock);
    idr_preload_end();

    if (err < 0) {
        kfree(batch);
        return err;
    }

    batch->batch_id = err;
    file->private_data = batch;

    return 0;
}

static int p2p_release(struct inode *inode, struct file *file)
{
    struct p2p_batch *batch = file->private_data;

    spin_lock(&batch_lock);
    idr_remove(&batch_tree, batch->batch_id);
    spin_unlock(&batch_lock);

    if (!list_empty(&batch->io_list) || batch->io_cnt > 0) {
        p2p_drain_read(batch);
    }

    kfree(batch);

    file->private_data = NULL;

    return 0;
}

static void init_process_id(const struct va_desc *desc, struct devmm_svm_process_id *process_id)
{
    memset(process_id, 0, sizeof(*process_id));
    rcu_read_lock();
    process_id->hostpid = pid_nr(find_vpid(desc->hostpid));
    rcu_read_unlock();
    process_id->devid = desc->devid;
    process_id->vfid = desc->vfid;
}

static void init_process_id_batch(const struct va_desc_ba *desc, struct devmm_svm_process_id *process_id)
{
    memset(process_id, 0, sizeof(*process_id));
    rcu_read_lock();
    process_id->hostpid = pid_nr(find_vpid(desc->hostpid));
    rcu_read_unlock();
    process_id->devid = desc->devid;
    process_id->vfid = desc->vfid;
}

static int query_page_size_with_diag(struct devmm_svm_process_id *process_id,
                                     int desc_hostpid, u64 addr, u64 size,
                                     bool is_batch)
{
    struct devmm_svm_process_id probe_id;
    int page_size;
    int probe0_ret = -999;

    if (is_batch) {
        diag_batch_hits++;
        diag_last_path = 2;
    } else {
        diag_single_hits++;
        diag_last_path = 1;
    }

    diag_desc_hostpid = desc_hostpid;
    diag_current_pid = current->pid;
    diag_current_tgid = current->tgid;
    diag_resolved_hostpid = process_id->hostpid;
    diag_devid = process_id->devid;
    diag_vfid = process_id->vfid;
    diag_probe0_ret = -999;

    page_size = devmm_get_mem_page_size(process_id, addr, size);
    diag_page_ret = page_size;

    /* Probe only.  Never use this result to issue I/O or obtain a PA list. */
    if (page_size <= 0 && process_id->devid != 0) {
        probe_id = *process_id;
        probe_id.devid = 0;
        probe0_ret = devmm_get_mem_page_size(&probe_id, addr, size);
        diag_probe0_ret = probe0_ret;
    }

    if (page_size <= 0) {
        pr_err("p2p_dev: SVM page query failed path=%s desc_hostpid=%d current_pid=%d current_tgid=%d resolved_hostpid=%d devid=%u vfid=%u addr=0x%llx size=%llu ret=%d probe0_ret=%d\n",
               is_batch ? "batch" : "single", desc_hostpid,
               current->pid, current->tgid, process_id->hostpid,
               process_id->devid, process_id->vfid, addr, size,
               page_size, probe0_ret);
    }

    return page_size;
}

#ifdef DUMP_CONTENT
static void dump_pa_content(unsigned long pa, unsigned int size)
{
    unsigned int data_len = min(size, 16U << 10);
    void *addr;

    addr = ioremap(pa, data_len);
    if (!addr) {
        return;
    }

    print_hex_dump(KERN_INFO, "PA %llx content: ", pa, DUMP_PREFIX_ADDRESS, 16, 1, addr, data_len, false);

    iounmap(addr);
}
#else
static void dump_pa_content(unsigned long pa, unsigned int size) {}
#endif

static int get_pa_list(const struct va_desc *desc, u64 **_pa_list, unsigned int *_pa_num, unsigned int *_pa_size)
{
    struct devmm_svm_process_id pid;
    u64 addr, aligned_addr;
    u64 size, aligned_size;
    u64 *pa_list;
    unsigned int pa_num;
    int page_size;
    int err;

    init_process_id(desc, &pid);

    addr = desc->addr;
    size = desc->size;
    page_size = query_page_size_with_diag(&pid, desc->hostpid, addr, size, false);
    if (page_size <= 0) {
        if (!page_size)
            page_size = -EINVAL;
        return page_size;
    }

    aligned_addr = round_down(addr, page_size);
    aligned_size = round_up((addr - aligned_addr + size), page_size);
    pa_num = aligned_size / page_size;

    pa_list = kvmalloc(pa_num * sizeof(*pa_list), GFP_KERNEL);
    if (!pa_list) {
        return -ENOMEM;
    }

    err = devmm_get_mem_pa_list(&pid, aligned_addr, aligned_size, pa_list, pa_num);
    if (err) {
        pr_err("p2p_dev: get PA list failed hostpid=%d devid=%u vfid=%u addr=0x%llx size=%llu pages=%u ret=%d\n",
               pid.hostpid, pid.devid, pid.vfid, aligned_addr, aligned_size, pa_num, err);
        kvfree(pa_list);
        return err;
    }

    devmm_put_mem_pa_list(&pid, aligned_addr, aligned_size, pa_list, pa_num);

    *_pa_list = pa_list;
    *_pa_num = pa_num;
    *_pa_size = page_size;

    return addr - aligned_addr;
}

static int get_pa_list_batch(const struct va_desc_ba *desc, u64 **_pa_list, unsigned int *_pa_num, unsigned int *_pa_size, int **_addr_off, int **_ret_size)
{
    struct devmm_svm_process_id pid;
    u64 addr, aligned_addr;
    u64 size, aligned_size;
    u64 tail;
    u64 *pa_list;
    int *addr_off;
    int *ret_size;
    unsigned int pa_num = 0;
    int page_size;
    int err;
    int i, j;

    init_process_id_batch(desc, &pid);

    addr = desc->addr[0];
    size = desc->size[0];
    page_size = query_page_size_with_diag(&pid, desc->hostpid, addr, size, true);
    if (page_size <= 0) {
        if (!page_size)
            page_size = -EINVAL;
        return page_size;
    }

    for (i = 0; i < desc->count; i++) {
        aligned_addr = round_down(desc->addr[i], page_size);
        aligned_size = round_up((desc->addr[i] - aligned_addr + desc->size[i]), page_size);
        pa_num += aligned_size / page_size;
    }

    pa_list = kvmalloc(pa_num * sizeof(*pa_list), GFP_KERNEL);
    if (!pa_list) {
        return -ENOMEM;
    }
    addr_off = kvmalloc(pa_num * sizeof(*addr_off), GFP_KERNEL);
    if (!addr_off) {
        kvfree(pa_list);
        return -ENOMEM;
    }
    ret_size = kvmalloc(pa_num * sizeof(*ret_size), GFP_KERNEL);
    if (!ret_size) {
        kvfree(pa_list);
        kvfree(addr_off);
        return -ENOMEM;
    }

    pa_num = 0;
    for (i = 0; i < desc->count; i++) {
        aligned_addr = round_down(desc->addr[i], page_size);
        aligned_size = round_up((desc->addr[i] - aligned_addr + desc->size[i]), page_size);
        pa_num += aligned_size / page_size;

        err = devmm_get_mem_pa_list(&pid, aligned_addr, aligned_size, pa_list + (pa_num - aligned_size / page_size), aligned_size / page_size);
        if (err) {
            pr_err("p2p_dev: batch get PA list failed hostpid=%d devid=%u vfid=%u index=%d addr=0x%llx size=%llu pages=%llu ret=%d\n",
                   pid.hostpid, pid.devid, pid.vfid, i, aligned_addr, aligned_size,
                   (unsigned long long)(aligned_size / page_size), err);
            kvfree(pa_list);
            kvfree(addr_off);
            kvfree(ret_size);
            return err;
        }
        devmm_put_mem_pa_list(&pid, aligned_addr, aligned_size, pa_list + (pa_num - aligned_size / page_size), aligned_size / page_size);
        j = pa_num - (aligned_size / page_size);
        addr_off[j] = desc->addr[i] - aligned_addr;
        ret_size[j] = min_t(int, desc->size[i], page_size - (desc->addr[i] - aligned_addr));
        j++;
        tail = (desc->addr[i] - aligned_addr + desc->size[i]) % page_size;
        while (j < pa_num) {
            addr_off[j] = 0;
            ret_size[j] = page_size;
            if (j == pa_num - 1) {
                ret_size[j] = tail ? tail : page_size;
            }
            j++;
        }
    }

    *_pa_list = pa_list;
    *_pa_num = pa_num;
    *_pa_size = page_size;
    *_addr_off = addr_off;
    *_ret_size = ret_size;

    return 0;
}

static int dump_pa(void __user *arg)
{
    struct va_desc desc;
    u64 *pa_list;
    unsigned int pa_num, pa_size;
    int err;

    if (copy_from_user(&desc, arg, sizeof(desc))) {
        return -EFAULT;
    }

    err = get_pa_list(&desc, &pa_list, &pa_num, &pa_size);
    if (err) {
        return err;
    }

    kvfree(pa_list);
    return 0;
}

static void free_io_ctx(struct p2p_io_context *io_ctx)
{
    kvfree(io_ctx->cmd_list);
    kvfree(io_ctx->pa_list);
    kfree(io_ctx);
}

static struct p2p_io_context *new_io_ctx(struct block_device *bdev, const struct read_desc *desc,
       u64 *pa_list, unsigned int pa_num, unsigned int pa_size, unsigned int data_size)
{
    struct p2p_io_context *io_ctx;
    unsigned int nr;

    io_ctx = kzalloc(sizeof(*io_ctx), GFP_KERNEL);
    if (!io_ctx) {
        return ERR_PTR(-ENOMEM);
    }

    nr = round_up(data_size, HW_LIMIT_SIZE) / HW_LIMIT_SIZE * 130;
    io_ctx->cmd_list = kvmalloc_array(nr, sizeof(*io_ctx->cmd_list), GFP_KERNEL);
    if (!io_ctx->cmd_list) {
        kfree(io_ctx);
        return ERR_PTR(-ENOMEM);
    }

    io_ctx->bdev = bdev;
    io_ctx->nsid = desc->nsid;
    io_ctx->pa_num = pa_num;
    io_ctx->pa_list = pa_list;
    io_ctx->pa_size = pa_size;
    io_ctx->data_size = data_size;
    io_ctx->count = nr;
    io_ctx->start_time = ktime_get_ns();

    atomic_set(&io_ctx->io_ref, 1);
    INIT_LIST_HEAD(&io_ctx->io_list);
    init_completion(&io_ctx->io_done);

    return io_ctx;
}

static struct p2p_io_context *new_io_ctx_batch(struct block_device *bdev, const struct read_desc_ba *desc,
       u64 *pa_list, unsigned int pa_num, unsigned int pa_size, unsigned int data_size)
{
    struct p2p_io_context *io_ctx;
    unsigned int limit = HW_LIMIT_SIZE;
    int count = data_size / limit * 10;
    if (count < desc->desc.count) {
        count = desc->desc.count * 10;
    }

    io_ctx = kzalloc(sizeof(*io_ctx), GFP_KERNEL);
    if (!io_ctx) {
        return ERR_PTR(-ENOMEM);
    }

    io_ctx->bdev = bdev;
    io_ctx->nsid = desc->nsid;
    io_ctx->pa_num = pa_num;
    io_ctx->pa_list = pa_list;
    io_ctx->pa_size = pa_size;
    io_ctx->cmd_list = kvmalloc_array(count, sizeof(struct nvme_command), GFP_KERNEL);
    if (!io_ctx->cmd_list) {
        kfree(io_ctx);
        return ERR_PTR(-ENOMEM);
    }
    io_ctx->start_time = ktime_get_ns();
    io_ctx->data_size = data_size;
    io_ctx->count = count;

    atomic_set(&io_ctx->io_ref, 1);
    INIT_LIST_HEAD(&io_ctx->io_list);
    init_completion(&io_ctx->io_done);

    return io_ctx;
}

static void hook_nvme_setup_cmd(void *ignore, struct request *rq, struct nvme_command *cmd)
{
    if (!(rq->rq_flags & RQF_NVME_PT)) {
        return;
    }

    cmd->rw.flags = NVME_CMD_SGL_METABUF;
}

static int register_nvme_setup_cmd_hook(void)
{
    tp_nvme_setup_cmd = (void *)tp_nvme_setup_cmd_addr;

    return tracepoint_probe_register(tp_nvme_setup_cmd, hook_nvme_setup_cmd, NULL);
}

#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 6, 0)
#define P2P_END_IO_RET enum rq_end_io_ret
#define P2P_END_IO_RETURN() return RQ_END_IO_NONE
#else
#define P2P_END_IO_RET void
#define P2P_END_IO_RETURN()
#endif

static P2P_END_IO_RET end_read_io(struct request *req, blk_status_t status)
{
    struct p2p_io_context *io_ctx = req->end_io_data;
    if (status)
        cmpxchg(&io_ctx->io_err, 0, status);

    blk_mq_free_request(req);
    io_ctx->end_time = ktime_get_ns();
    if (!atomic_dec_and_test(&io_ctx->io_ref)) {
        P2P_END_IO_RETURN();
    }

    complete(&io_ctx->io_done);
    P2P_END_IO_RETURN();
}

#define THRESH_NS (1000000UL)

static int do_read_io(struct p2p_io_context *io_ctx, unsigned long long sector, unsigned int sector_nr, unsigned long long paddr)
{
    struct gendisk *disk = io_ctx->bdev->bd_disk;
    struct request_queue *queue = disk->queue;
    struct request *req;
    struct nvme_command *cmd = &io_ctx->cmd_list[io_ctx->cmd_id];
    if (io_ctx->cmd_id >= io_ctx->count) {
        pr_err("cmd_id %d >= count %d\n", io_ctx->cmd_id, io_ctx->count);
        return -EINVAL;
    }

    pr_debug("read from blk 0x%x0x%llx to pa 0x%llx\n", sector_nr, sector, paddr);

    cmd->rw.opcode = nvme_cmd_read;
    cmd->rw.nsid = cpu_to_le32(io_ctx->nsid);
    cmd->rw.slba = cpu_to_le64(sector);
    cmd->rw.length = cpu_to_le16(sector_nr - 1);
    cmd->rw.control = 0;
    cmd->rw.dsmgmt = 0;

    cmd->rw.dptr.sgl.addr = cpu_to_le64(paddr);
    cmd->rw.dptr.sgl.length = cpu_to_le32(sector_nr << SECTOR_SHIFT);
    cmd->rw.dptr.sgl.type = NVME_SGL_FMT_DATA_DESC << 4;
    io_ctx->cmd_id++;

#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 6, 0)
    /*
     * nvme_alloc_request() no longer exists on 6.6.  This request is an
     * ordinary NVMe I/O passthrough request; qid == -1 in the old call means
     * that blk-mq may choose the hardware queue.
     */
    req = blk_mq_alloc_request(queue,
            cmd->rw.opcode == nvme_cmd_write ? REQ_OP_DRV_OUT : REQ_OP_DRV_IN,
            0);
    if (!IS_ERR(req))
        nvme_init_request(req, cmd);
#else
    req = nvme_alloc_request(queue, cmd, 0, -1);
#endif
    if (IS_ERR(req)) {
        pr_err("Failed to alloc request\n");
        return -ENOMEM;
    }

    req->rq_flags |= RQF_NVME_PT;
    req->end_io_data = io_ctx;
    atomic_inc(&io_ctx->io_ref);
    
#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 6, 0)
    /*
     * Since Linux 6.6, blk_execute_rq_nowait() takes only the request and
     * the queue-head flag.  The completion callback is carried by the
     * request itself.
     */
    req->end_io = end_read_io;
    blk_execute_rq_nowait(req, true);
#elif LINUX_VERSION_CODE >= KERNEL_VERSION(5, 9, 0)
    blk_execute_rq_nowait(disk, req, true, end_read_io);
#else
    blk_execute_rq_nowait(queue, disk, req, true, end_read_io);
#endif

    return 0;
}

static unsigned int cur_pa_remain_sector(struct p2p_io_context *io_ctx)
{
    if (io_ctx->pa_idx >= io_ctx->pa_num) {
        pr_warn("bad pa_idx %d >= pa_num %d\n", io_ctx->pa_idx, io_ctx->pa_num);
        return 0;
    }
    return (io_ctx->pa_size - io_ctx->pa_offset) >> SECTOR_SHIFT;
}

static unsigned long long cur_pa(struct p2p_io_context *io_ctx)
{
    return io_ctx->pa_list[io_ctx->pa_idx] + io_ctx->pa_offset;
}

static void cur_pa_advance_sector(struct p2p_io_context *io_ctx, unsigned int sector)
{
    io_ctx->pa_offset += (sector << SECTOR_SHIFT);
    if (io_ctx->pa_offset >= io_ctx->pa_size) {
        if (io_ctx->pa_offset > io_ctx->pa_size) {
            pr_warn("bad pa_offset %u > pa_size %u\n", io_ctx->pa_offset, io_ctx->pa_size);
        }
        io_ctx->pa_offset = 0;
        io_ctx->pa_idx++;
    }
}

static unsigned int calc_read_size(struct p2p_io_context *io_ctx, unsigned int left)
{
    const unsigned int limit = (128 << 10) >> SECTOR_SHIFT;
    unsigned int to_read = left;
    unsigned int pa_left;

    if (to_read > limit)
        to_read = limit;
    
    pa_left = cur_pa_remain_sector(io_ctx);
    if (to_read > pa_left)
        to_read = pa_left;
    return to_read;
}

static int do_read_ios(struct p2p_io_context *io_ctx, struct fiemap_extent *extents, unsigned int nr)
{
    unsigned int i;
    int err = 0;

    for (i = 0; i < nr; i++) {
        unsigned long long sector = extents[i].fe_physical >> SECTOR_SHIFT;
        unsigned int left = extents[i].fe_length >> SECTOR_SHIFT;
        unsigned int to_read;
        int err;

        while (left > 0) {
            to_read = calc_read_size(io_ctx, left);
            if (!to_read) {
                err = -EINVAL;
                break;
            }
            err = do_read_io(io_ctx, sector, to_read, cur_pa(io_ctx));
            if (err) {
                goto out;
            }
            sector += to_read;
            left -= to_read;
            cur_pa_advance_sector(io_ctx, to_read);
        }
    }

out:
    return err;
}

static int do_read_ios_batch(struct p2p_io_context *io_ctx, struct fiemap_extent *extents, unsigned int nr,
       int *addr_off, int *align_size)
{
    unsigned int i;
    int err = 0;
    unsigned long long count = 0;
    unsigned long long size = align_size[0] >> SECTOR_SHIFT;
    int idx = 0;
    io_ctx->pa_offset = addr_off[0];

    for (i = 0; i < nr; i++) {
        unsigned long long sector = extents[i].fe_physical >> SECTOR_SHIFT;
        unsigned int left = extents[i].fe_length >> SECTOR_SHIFT;
        unsigned int to_read;
        int err;

        while (left > 0) {
            to_read = calc_read_size(io_ctx, left);
            if (!to_read) {
                err = -EINVAL;
                break;
            }

            if (count + to_read > size) {
                to_read = size - count;
                err = do_read_io(io_ctx, sector, to_read, cur_pa(io_ctx));
                if (err) {
                    goto out;
                }
                io_ctx->pa_idx++;
                idx++;
                io_ctx->pa_offset = addr_off[idx];
                size = align_size[idx] >> SECTOR_SHIFT;
                count = 0;
                sector += to_read;
                left -= to_read;
                continue;
            }

            err = do_read_io(io_ctx, sector, to_read, cur_pa(io_ctx));
            if (err) {
                goto out;
            }
            sector += to_read;
            left -= to_read;
            count += to_read;
            if (count >= size) {
                io_ctx->pa_idx++;
                idx++;
                io_ctx->pa_offset = addr_off[idx];
                size = align_size[idx] >> SECTOR_SHIFT;
                count = 0;
            } else
                cur_pa_advance_sector(io_ctx, to_read);
        }
    }

out:
    return err;
}

static int wait_io_done(struct p2p_io_context *io_ctx)
{
    if (!atomic_dec_and_test(&io_ctx->io_ref)) {
        wait_for_completion_io(&io_ctx->io_done);
    }
    return io_ctx->io_err;
}

static unsigned long long calc_data_size(struct fiemap_extent *extents, unsigned int nr)
{
    unsigned int i;
    unsigned long long size = 0;

    for (i = 0; i < nr; i++) {
        size += extents[i].fe_length;
    }
    return size;
}

static int p2p_read_file(struct p2p_batch *batch, void __user *arg)
{
    struct read_desc __user *user_desc = arg;
    struct fiemap_extent *extents;
    struct read_desc desc;
    struct file *reg_file;
    struct file *bdev_file;
    struct inode *bdev_inode;
    struct block_device *bdev;
    struct p2p_io_context *io_ctx;
    u64 *pa_list;
    u64 data_size;
    unsigned int pa_num;
    unsigned int pa_size;
    unsigned int ext_num;
    int err;
    int addr_off;

    if (copy_from_user(&desc, user_desc, sizeof(desc))) {
        return -EFAULT;
    }

    addr_off = get_pa_list(&desc.desc, &pa_list, &pa_num, &pa_size);
    if (addr_off < 0) {
        pr_err("p2p_dev: READ_FILE rejected during PA query hostpid=%d devid=%u vfid=%u addr=0x%lx size=%lu ret=%d\n",
               desc.desc.hostpid, desc.desc.devid, desc.desc.vfid,
               desc.desc.addr, desc.desc.size, addr_off);
        return addr_off;
    }
    pr_info("p2p_dev: READ_FILE request hostpid=%d devid=%u vfid=%u addr=0x%lx size=%lu pa_num=%u pa_size=%u addr_off=%d file_fd=%d bdev_fd=%d ext_num=%u\n",
            desc.desc.hostpid, desc.desc.devid, desc.desc.vfid,
            desc.desc.addr, desc.desc.size, pa_num, pa_size, addr_off,
            desc.file_fd, desc.bdev_fd, desc.ext_num);

    reg_file = fget(desc.file_fd);
    if (!reg_file) {
        err = -EBADF;
        pr_err("p2p_dev: READ_FILE invalid file_fd=%d ret=%d\n", desc.file_fd, err);
        goto free_pa_out;
    }

    bdev_file = fget(desc.bdev_fd);
    if (!bdev_file) {
        err = -EBADF;
        pr_err("p2p_dev: READ_FILE invalid bdev_fd=%d ret=%d\n", desc.bdev_fd, err);
        goto put_reg_file_out;
    }
    bdev_inode = bdev_file->f_mapping->host;
    if (!S_ISBLK(bdev_inode->i_mode)) {
        err = -EINVAL;
        pr_err("p2p_dev: READ_FILE bdev_fd=%d is not a block device mode=0%o ret=%d\n",
               desc.bdev_fd, bdev_inode->i_mode, err);
        goto put_bdev_file_out;
    }
    bdev = I_BDEV(bdev_inode);

    ext_num = desc.ext_num;
    extents = kvmalloc(ext_num * sizeof(*extents), GFP_KERNEL);
    if (!extents) {
        err = -ENOMEM;
        goto put_bdev_file_out;
    }
    if (copy_from_user(extents, user_desc->extents, ext_num * sizeof(*extents))) {
        err = -EFAULT;
        pr_err("p2p_dev: READ_FILE extent copy failed ext_num=%u ret=%d\n", ext_num, err);
        goto free_ext_out;
    }

    data_size = calc_data_size(extents, ext_num);
    if (data_size > (unsigned long long)pa_size * pa_num) {
        pr_err("p2p_dev: READ_FILE data exceeds NPU buffer data_size=%llu pa_size=%u pa_num=%u ret=%d\n",
               data_size, pa_size, pa_num, -E2BIG);
        err = -E2BIG;
        goto free_ext_out;
    }

    io_ctx = new_io_ctx(bdev, &desc, pa_list, pa_num, pa_size, data_size);
    if (IS_ERR(io_ctx)) {
        err = PTR_ERR(io_ctx);
        goto free_ext_out;
    }
    pa_list = NULL;
    io_ctx->pa_offset = addr_off;

    io_ctx->issue_err = do_read_ios(io_ctx, extents, ext_num);
    if (io_ctx->issue_err)
        pr_err("p2p_dev: READ_FILE request issue failed hostpid=%d devid=%u vfid=%u ret=%d cmd_count=%d\n",
               desc.desc.hostpid, desc.desc.devid, desc.desc.vfid,
               io_ctx->issue_err, io_ctx->cmd_id);
    
    spin_lock(&batch->io_lock);
    batch->io_cnt++;
    list_add_tail(&io_ctx->io_list, &batch->io_list);
    spin_unlock(&batch->io_lock);

free_ext_out:
    kvfree(extents);
put_bdev_file_out:
    fput(bdev_file);
put_reg_file_out:
    fput(reg_file);
free_pa_out:
    kvfree(pa_list);
    return err;
}

static int p2p_drain_read(struct p2p_batch *batch)
{
    struct p2p_io_context *io_ctx, *next_io_ctx;
    unsigned int total_cnt;
    unsigned int got_cnt;
    unsigned int err_cnt;
    int first_err = 0;
    LIST_HEAD(tmp);

    if (!READ_ONCE(batch->io_cnt))
        return 0;

    spin_lock(&batch->io_lock);
    list_splice_init(&batch->io_list, &tmp);
    total_cnt = batch->io_cnt;
    batch->io_cnt = 0;
    spin_unlock(&batch->io_lock);

    if (total_cnt == 0)
        return 0;

    err_cnt = 0;
    got_cnt = 0;
    list_for_each_entry_safe(io_ctx, next_io_ctx, &tmp, io_list) {
        int err, io_err;

        got_cnt++;
        err = io_ctx->issue_err;
        io_err = wait_io_done(io_ctx);
        
        if (io_err && !err) {
            pr_err("got io err %d %d %d\n", io_err, blk_status_to_errno(io_err), io_ctx->nsid);
            /* Return a negative errno to the userspace wrapper, rather than
             * the positive blk_status_t enum value. */
            err = blk_status_to_errno(io_err);
        }

        if (!err)
            dump_pa_content(io_ctx->pa_list[0], min_t(unsigned int, io_ctx->data_size, io_ctx->pa_size));
        else {
            err_cnt++;
            if (!first_err)
                first_err = err;
        }

        if (io_ctx->cmd_id != 0) {
            g_time += (io_ctx->end_time - io_ctx->start_time);
            g_size += io_ctx->data_size;
            g_count++;
        } else {
            pr_info("cmd_id 0 io_cnt %d\n", io_ctx->cmd_id);
        }

        list_del_init(&io_ctx->io_list);
        free_io_ctx(io_ctx);
    }

    if (g_count >= 1000) {
        pr_info("end drain %u read got cnt %u/%u/%llu io %llu/%llu bandwidth %llu\n",
                batch->batch_id, got_cnt, err_cnt,
                (unsigned long long)g_count, (unsigned long long)g_size,
                (unsigned long long)g_time,
                (unsigned long long)(g_time ? g_size / g_time : 0));
        g_time = 0;
        g_size = 0;
        g_count = 0;
    }
    return first_err;
}

static int p2p_read_file_batch(struct p2p_batch *batch, void __user *arg)
{
    struct read_desc_ba __user *user_desc = arg;
    struct fiemap_extent *extents;
    unsigned long *kaddr, *kaddr1;
    struct read_desc_ba desc;
    struct file *reg_file;
    struct file *bdev_file;
    struct inode *bdev_inode;
    struct block_device *bdev;
    struct p2p_io_context *io_ctx;
    u64 *pa_list;
    u64 data_size;
    unsigned int pa_num;
    unsigned int pa_size;
    unsigned int ext_num;
    int err;
    int *addr_off;
    int *align_size;

    if (copy_from_user(&desc, user_desc, sizeof(desc))) {
        return -EFAULT;
    }
    kaddr = kvmalloc(desc.desc.count * sizeof(unsigned long), GFP_KERNEL);
    if (!kaddr)
        return -ENOMEM;
    if (copy_from_user(kaddr, (__force void __user *)(uintptr_t)desc.desc.addr, desc.desc.count * sizeof(unsigned long))) {
        err = -EFAULT;
        goto free_kaddr_out;
    }
    desc.desc.addr = kaddr;
    
    kaddr1 = kvmalloc(desc.desc.count * sizeof(unsigned long), GFP_KERNEL);
    if (!kaddr1) {
        err = -ENOMEM;
        goto free_kaddr_out;
    }
    if (copy_from_user(kaddr1, (__force void __user *)(uintptr_t)desc.desc.size, desc.desc.count * sizeof(unsigned long))) {
        err = -EFAULT;
        goto free_kaddr1_out;
    }
    desc.desc.size = kaddr1;

    err = get_pa_list_batch(&desc.desc, &pa_list, &pa_num, &pa_size, &addr_off, &align_size);
    if (err) {
        err = -EFAULT;
        goto free_kaddr1_out;
    }

    reg_file = fget(desc.file_fd);
    if (!reg_file) {
        err = -EBADF;
        goto free_pa_out;
    }

    bdev_file = fget(desc.bdev_fd);
    if (!bdev_file) {
        err = -EBADF;
        goto put_reg_file_out;
    }
    bdev_inode = bdev_file->f_mapping->host;
    if (!S_ISBLK(bdev_inode->i_mode)) {
        err = -EINVAL;
        goto put_bdev_file_out;
    }
    bdev = I_BDEV(bdev_inode);

    ext_num = desc.ext_num;
    extents = kvmalloc(ext_num * sizeof(*extents), GFP_KERNEL);
    if (!extents) {
        err = -ENOMEM;
        goto put_bdev_file_out;
    }
    if (copy_from_user(extents, user_desc->extents, ext_num * sizeof(*extents))) {
        err = -EFAULT;
        goto free_ext_out;
    }

    data_size = calc_data_size(extents, ext_num);
    if (data_size > (unsigned long long)pa_size * pa_num) {
        pr_err("data_size %llu > pa_size %u * pa_num %u\n", data_size, pa_size, pa_num);
        err = -E2BIG;
        goto free_ext_out;
    }

    io_ctx = new_io_ctx_batch(bdev, &desc, pa_list, pa_num, pa_size, data_size);
    if (IS_ERR(io_ctx)) {
        err = PTR_ERR(io_ctx);
        goto free_ext_out;
    }
    pa_list = NULL;

    io_ctx->issue_err = do_read_ios_batch(io_ctx, extents, ext_num, addr_off, align_size);
    
    spin_lock(&batch->io_lock);
    batch->io_cnt++;
    list_add_tail(&io_ctx->io_list, &batch->io_list);
    spin_unlock(&batch->io_lock);

free_ext_out:
    kvfree(extents);
put_bdev_file_out:
    fput(bdev_file);
put_reg_file_out:
    fput(reg_file);
free_pa_out:
    kvfree(pa_list);
    kvfree(addr_off);
    kvfree(align_size);
free_kaddr1_out:
    kvfree(kaddr1);
free_kaddr_out:
    kvfree(kaddr);
    return err;
}

static long p2p_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
    struct p2p_batch *batch = file->private_data;
    int err = 0;

    switch(cmd) {
    case IOCTL_DUMP_PA:
        err = dump_pa((void __user *)arg);
        break;
    case IOCTL_READ_FILE:
        err = p2p_read_file(batch, (void __user *)arg);
        break;
    case IOCTL_READ_FILE_BATCH:
        err = p2p_read_file_batch(batch, (void __user *)arg);
        break;
    case IOCTL_DRAIN_READ:
        err = p2p_drain_read(batch);
        break;
    default:
        pr_info("p2p driver: invalid ioctl command 0x%x\n", cmd);
        err = -EINVAL;
        break;
    }
    return err;
}

static int __init p2p_drv_init(void)
{
    struct device *device;
    int err;

    if (!tp_nvme_setup_cmd_addr) {
        pr_err("set tp_block_rq_issue_addr=???\n");
        return -EINVAL;
    }

    err = alloc_chrdev_region(&dev, 0, 1, "p2p_device");
    if (err < 0) {
        pr_err("p2p Driver: cannot allocate major number err %d\n", err);
        return err;
    }

    pr_info("p2p Driver: Major = %d Minor = %d \n", MAJOR(dev), MINOR(dev));
    cdev_init(&p2p_cdev, &fops);

    err = cdev_add(&p2p_cdev, dev, 1);
    if (err < 0) {
        pr_err("p2p Driver: Cannot add the device to the system. \n");
        goto free_dev;
    }

#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 4, 0)
    dev_class = class_create("p2p_class");
#else
    dev_class = class_create(THIS_MODULE, "p2p_class");
#endif
    if (IS_ERR(dev_class)) {
        err = PTR_ERR(dev_class);
        pr_err("p2p Driver: Cannot create the struct class\n");
        goto del_cdev;
    }

    device = device_create(dev_class, NULL, dev, NULL, "p2p_device");
    if (IS_ERR(device)) {
        err = PTR_ERR(device);
        pr_err("p2p Driver: Cannot create the device\n");
        goto del_cls;
    }

    err = register_nvme_setup_cmd_hook();
    if (err) {
        pr_err("p2p Driver: register rq_issue hook err %d\n", err);
        goto del_dev;
    }

    pr_info("p2p Driver: Device Driver Inserted Done\n");
    return 0;

del_dev:
    device_destroy(dev_class, dev);
del_cls:
    class_destroy(dev_class);
del_cdev:
    cdev_del(&p2p_cdev);
free_dev:
    unregister_chrdev_region(dev, 1);
    return err;
}

static void __exit p2p_drv_exit(void)
{
    tracepoint_probe_unregister(tp_nvme_setup_cmd, hook_nvme_setup_cmd, NULL);

    device_destroy(dev_class, dev);
    class_destroy(dev_class);
    cdev_del(&p2p_cdev);
    unregister_chrdev_region(dev, 1);
    pr_info("p2p Driver: Device Driver Removed Done\n");
}

module_init(p2p_drv_init);
module_exit(p2p_drv_exit);
MODULE_LICENSE("GPL");
