package com.plopvape.product.repository;

import com.plopvape.product.entity.Product;
import org.springframework.data.jpa.repository.JpaRepository;
import java.util.List;

public interface ProductRepository extends JpaRepository<Product, Long> {

    List<Product> findByCategoryId(Long categoryId);

    List<Product> findByNameContainingIgnoreCase(String name);

    List<Product> findByCategoryIdAndNameContainingIgnoreCase(Long categoryId, String name);
}
