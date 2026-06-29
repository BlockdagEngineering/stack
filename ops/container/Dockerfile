FROM debian:12-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    ca-certificates \
    coreutils \
    findutils \
    postgresql-client-15 \
    rsync \
    util-linux \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY bin/ /app/bin/
COPY lib/ /app/lib/

RUN chmod 0755 /app/bin/vqueen-container-backup.sh

ENTRYPOINT ["/app/bin/vqueen-container-backup.sh"]
