package com.acme.warehouse.inventory;

import java.time.Instant;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.locks.ReentrantLock;

import com.acme.warehouse.audit.AuditTrail;
import com.acme.warehouse.model.Sku;
import com.acme.warehouse.model.StockLevel;

/**
 * Coordinates stock reservations across the warehouse.
 *
 * <p>Reservations are two-phase: {@link #reserveStock} takes a soft hold that
 * expires after {@code holdSeconds}, and {@link #confirmReservation} converts
 * the hold into a committed decrement. This is what keeps two concurrent
 * checkouts from overselling the last unit of a SKU.
 */
public final class InventoryService {

    private static final int DEFAULT_HOLD_SECONDS = 900;

    private final Map<Sku, StockLevel> levels = new HashMap<>();
    private final Map<String, Reservation> reservations = new HashMap<>();
    private final ReentrantLock lock = new ReentrantLock();
    private final AuditTrail auditTrail;
    private final int holdSeconds;

    public InventoryService(AuditTrail auditTrail) {
        this(auditTrail, DEFAULT_HOLD_SECONDS);
    }

    public InventoryService(AuditTrail auditTrail, int holdSeconds) {
        if (holdSeconds <= 0) {
            throw new IllegalArgumentException("holdSeconds must be positive");
        }
        this.auditTrail = auditTrail;
        this.holdSeconds = holdSeconds;
    }

    /**
     * Places a soft hold on {@code quantity} units of {@code sku}.
     *
     * @return the reservation id, or empty when there is not enough stock
     */
    public Optional<String> reserveStock(Sku sku, int quantity) {
        if (quantity <= 0) {
            throw new IllegalArgumentException("quantity must be positive: " + quantity);
        }
        lock.lock();
        try {
            StockLevel level = levels.get(sku);
            if (level == null || level.available() < quantity) {
                auditTrail.record("reserve.rejected", sku.code(), quantity);
                return Optional.empty();
            }
            level.hold(quantity);
            String reservationId = sku.code() + "-" + Instant.now().toEpochMilli();
            reservations.put(reservationId, new Reservation(sku, quantity, Instant.now()));
            auditTrail.record("reserve.accepted", sku.code(), quantity);
            return Optional.of(reservationId);
        } finally {
            lock.unlock();
        }
    }

    /**
     * Turns a soft hold into a committed decrement of the stock level.
     */
    public boolean confirmReservation(String reservationId) {
        lock.lock();
        try {
            Reservation reservation = reservations.remove(reservationId);
            if (reservation == null || reservation.isExpired(holdSeconds)) {
                return false;
            }
            StockLevel level = levels.get(reservation.sku());
            level.commit(reservation.quantity());
            auditTrail.record("reserve.confirmed", reservation.sku().code(), reservation.quantity());
            return true;
        } finally {
            lock.unlock();
        }
    }

    /**
     * Releases an unconfirmed hold so the units become available again.
     */
    public void releaseStock(String reservationId) {
        lock.lock();
        try {
            Reservation reservation = reservations.remove(reservationId);
            if (reservation == null) {
                return;
            }
            levels.get(reservation.sku()).release(reservation.quantity());
            auditTrail.record("reserve.released", reservation.sku().code(), reservation.quantity());
        } finally {
            lock.unlock();
        }
    }

    /**
     * Adds incoming units from a supplier delivery.
     */
    public void applyRestock(Sku sku, int quantity, String purchaseOrder) {
        lock.lock();
        try {
            levels.computeIfAbsent(sku, key -> new StockLevel(key, 0)).add(quantity);
            auditTrail.record("restock", sku.code(), quantity);
        } finally {
            lock.unlock();
        }
    }

    /**
     * Lists every SKU whose available quantity has fallen below its reorder point.
     */
    public List<Sku> findLowStock() {
        lock.lock();
        try {
            return levels.values().stream()
                    .filter(StockLevel::belowReorderPoint)
                    .map(StockLevel::sku)
                    .toList();
        } finally {
            lock.unlock();
        }
    }

    /**
     * Drops holds that outlived {@code holdSeconds}. Called by the sweeper job.
     */
    public int expireStaleReservations() {
        lock.lock();
        try {
            int expired = 0;
            for (Map.Entry<String, Reservation> entry : Map.copyOf(reservations).entrySet()) {
                if (entry.getValue().isExpired(holdSeconds)) {
                    releaseStock(entry.getKey());
                    expired++;
                }
            }
            return expired;
        } finally {
            lock.unlock();
        }
    }

    /**
     * Immutable record of a soft hold.
     */
    private record Reservation(Sku sku, int quantity, Instant createdAt) {

        boolean isExpired(int holdSeconds) {
            return createdAt.plusSeconds(holdSeconds).isBefore(Instant.now());
        }
    }
}
