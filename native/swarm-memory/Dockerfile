# Build against the oldest fleet runtime (Debian 12 / glibc 2.36).
# The LAN-first build helper resolves this base to an immutable local image.
FROM genomeharbor/epigenetic-pilot:20261113
USER root
RUN apt-get update && apt-get install -y --no-install-recommends g++ cmake libfuse3-dev libssl-dev nlohmann-json3-dev && rm -rf /var/lib/apt/lists/*
RUN apt-get update && apt-get install -y --no-install-recommends make && rm -rf /var/lib/apt/lists/*
COPY . /src
RUN cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release && cmake --build /build -j2
