import sys
import file_p2p

def test_read(name):
    dev_fd = file_p2p.new_p2p_fd()
    if dev_fd < 0:
        print("new_p2p_fd failed, errno: %d" % -dev_fd)
        return

    err = file_p2p.read_file(dev_fd, name, "/dev/nvme0n1", 4096, 0, 8 << 20, 0, 0)
    if err < 0:
        print("read_file failed, errno: %d" % -err)
        return
    err = file_p2p.drain_read(dev_fd)
    if err < 0:
        print("drain_read failed, errno: %d" % -err)
        return

if __name__ == "__main__":
    test_read(sys.argv[1])