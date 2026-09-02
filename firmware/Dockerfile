# Build context: the firmware/ directory.
#   docker build -t ecu-agent firmware
#   docker run --rm --network host ecu-agent --id ECU-0001 --host 127.0.0.1
FROM debian:bookworm-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY CMakeLists.txt ./
COPY include ./include
COPY src ./src
COPY tests ./tests
RUN cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release \
 && cmake --build /build -j"$(nproc)" \
 && ctest --test-dir /build --output-on-failure

FROM debian:bookworm-slim
COPY --from=build /build/ecu_agent /ecu_agent
ENTRYPOINT ["/ecu_agent"]
