# One image for the sr3 subscribers and the ingest job.
#
# Built from the same file on the laptop (Intel/AMD) and the AWS VM (ARM):
# the micromamba base image and every conda-forge package exist for both.
# docker-compose.yml decides which program each container runs.

FROM mambaorg/micromamba:2.10.0-ubuntu24.04

# Install the Python environment into the image's base env.
COPY --chown=$MAMBA_USER:$MAMBA_USER environment.yml /tmp/environment.yml
RUN micromamba install -y -n base -f /tmp/environment.yml \
    && micromamba clean --all --yes

# Containers run as user id 1000 (set in docker-compose.yml), which owns
# ./data on both the laptop and the VM. Then files sr3 downloads into
# the shared data folder belong to us, not to root.
# /home/gbw is that user's home: sr3 keeps state in ~/.cache/sr3 and
# reads configs from ~/.config/sr3.
USER root
RUN mkdir -p /home/gbw/.cache/sr3 /home/gbw/.config/sr3 \
    && chown -R 1000:1000 /home/gbw \
    && (getent passwd 1000 >/dev/null || useradd -u 1000 -d /home/gbw gbw)
ENV HOME=/home/gbw

# Our code. Data, secrets and git history stay out (see .dockerignore).
WORKDIR /app
COPY config/ config/
COPY ingest/ ingest/

# Print log lines straight away instead of buffering them, so
# `docker compose logs` shows them as they happen.
ENV PYTHONUNBUFFERED=1

USER 1000:1000
