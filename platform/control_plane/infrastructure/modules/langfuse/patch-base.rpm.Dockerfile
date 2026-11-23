# Apply the base-OS security updates to an image that carries an rpm database and no package
# manager: Langfuse v4 ships on UBI micro, which has neither dnf nor rpm. dnf runs in a full UBI
# stage against a copy of the image's root, and only the paths it changed are layered back, dnf's
# own cache, history and logs aside, so the result keeps upstream's configuration and grows by
# the updated packages alone.

ARG BASE
ARG UBI=registry.access.redhat.com/ubi9/ubi

FROM ${BASE} AS upstream

FROM ${UBI} AS patcher
COPY --from=upstream / /mnt/rootfs
RUN set -eu; \
    target="$(rpm --root /mnt/rootfs -q --qf '%{VERSION}' redhat-release | cut -d. -f1)"; \
    builder="$(rpm -q --qf '%{VERSION}' redhat-release | cut -d. -f1)"; \
    if [ "$target" != "$builder" ]; then \
        echo "patch-base.rpm: the image is RHEL $target and this stage is RHEL $builder;" \
             "build with --build-arg UBI=registry.access.redhat.com/ubi$target/ubi" >&2; \
        exit 1; \
    fi; \
    cd /mnt/rootfs; \
    find . -xdev \( -type f -o -type l \) -printf '%p\t%C@\n' | sort > /tmp/before; \
    dnf --installroot /mnt/rootfs --releasever "$target" --setopt=reposdir=/etc/yum.repos.d \
        --setopt=install_weak_deps=false --nodocs -y update; \
    find . -xdev \( -type f -o -type l \) -printf '%p\t%C@\n' | sort > /tmp/after; \
    cut -f1 /tmp/before | sort > /tmp/before.paths; \
    cut -f1 /tmp/after | sort > /tmp/after.paths; \
    dnf_state='\#^\./var/(cache/(dnf|yum)|lib/dnf|log)/#d'; \
    mkdir /mnt/delta; \
    comm -13 /tmp/before /tmp/after | cut -f1 | sed -E "$dnf_state" | tar -cf - -T - | tar -xf - -C /mnt/delta; \
    comm -23 /tmp/before.paths /tmp/after.paths | sed -E "$dnf_state" > /mnt/delta/.patch-base-removed

FROM ${BASE}

ARG ORIG_USER=root

USER root
COPY --from=patcher /mnt/delta/ /
RUN while IFS= read -r path; do rm -f "/$path"; done < /.patch-base-removed; \
    rm -f /.patch-base-removed

USER ${ORIG_USER}
