FROM fedora:42

RUN dnf install -y libxcb python3 gh

RUN curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh |\
  GOOSE_BIN_DIR=/usr/local/bin CONFIGURE=false bash

RUN useradd -m -G wheel goose && sed -i '/NOPASSWD/s/^# //' /etc/sudoers

RUN mkdir -p /home/goose/{.config/goose,recipes} && chown -R goose:goose /home/goose

USER goose
WORKDIR /home/goose

ENTRYPOINT ["/usr/local/bin/goose"]
