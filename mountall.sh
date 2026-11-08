sudo mount -t cifs //192.168.1.182/sortmyfiles /mnt/cfo211/sortmyfiles -o credentials=/home/tof/Projects/photoprocess/smbcredentials,vers=3.0,iocharset=utf8
sudo mount -t cifs //192.168.1.182/photo /mnt/cfo211/photo -o credentials=/home/tof/Projects/photoprocess/smbcredentials,vers=3.0,iocharset=utf8
sudo mount -t cifs //192.168.1.182/video /mnt/cfo211/video -o credentials=/home/tof/Projects/photoprocess/smbcredentials,vers=3.0,iocharset=utf8
sudo mount -t cifs //192.168.1.21/media /mnt/cfo216/media -o credentials=/home/tof/Projects/photoprocess/smbcredentials,vers=3.0,iocharset=utf8
sudo mount -t ntfs3 -o ro /dev/sdb2 /mnt/ironman/
sudo mount -t cifs //192.168.1.21/media_reference /mnt/media_reference  -o "credentials=/home/tof/Projects/photoprocess/smbcredentials_photoprocess_rw,vers=3.0,iocharset=utf8,uid=$(id -u),gid=$(id -g),file_mode=0664,dir_mode=0775"