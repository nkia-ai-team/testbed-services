package com.corebanking.transfer.repository;

import com.corebanking.transfer.entity.Transfer;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;

public interface TransferRepository extends JpaRepository<Transfer, Long> {
    Optional<Transfer> findByTransferRef(String transferRef);
}
