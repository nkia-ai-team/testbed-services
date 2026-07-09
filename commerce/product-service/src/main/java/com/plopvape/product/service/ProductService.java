package com.plopvape.product.service;

import com.plopvape.common.dto.ProductResponse;
import com.plopvape.common.dto.ReserveStockResponse;
import com.plopvape.common.exception.ServiceException;
import com.plopvape.product.client.InventoryClient;
import com.plopvape.product.entity.Product;
import com.plopvape.product.repository.ProductRepository;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

import java.util.List;

@Service
public class ProductService {

    private final ProductRepository productRepository;
    private final InventoryClient inventoryClient;

    public ProductService(ProductRepository productRepository, InventoryClient inventoryClient) {
        this.productRepository = productRepository;
        this.inventoryClient = inventoryClient;
    }

    public List<ProductResponse> getProducts(Long categoryId, String name) {
        List<Product> products;
        if (categoryId != null && name != null) {
            products = productRepository.findByCategoryIdAndNameContainingIgnoreCase(categoryId, name);
        } else if (categoryId != null) {
            products = productRepository.findByCategoryId(categoryId);
        } else if (name != null) {
            products = productRepository.findByNameContainingIgnoreCase(name);
        } else {
            products = productRepository.findAll();
        }
        return products.stream().map(this::toResponse).toList();
    }

    public ProductResponse getProductWithStock(Long id) {
        Product product = findById(id);
        int stock = inventoryClient.getStock(id);
        return new ProductResponse(
                product.getId(),
                product.getName(),
                product.getDescription(),
                product.getPrice(),
                product.getCategory() != null ? product.getCategory().getName() : null,
                product.getImageUrl(),
                stock
        );
    }

    public ReserveStockResponse reserveStock(Long productId, int quantity) {
        Product product = findById(productId);
        var inventoryResponse = inventoryClient.reserve(productId, quantity);
        return new ReserveStockResponse(
                product.getId(),
                product.getName(),
                product.getPrice(),
                inventoryResponse.reserved(),
                inventoryResponse.remainingStock()
        );
    }

    private Product findById(Long id) {
        return productRepository.findById(id)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND,
                        "Product not found: " + id));
    }

    private ProductResponse toResponse(Product product) {
        return new ProductResponse(
                product.getId(),
                product.getName(),
                product.getDescription(),
                product.getPrice(),
                product.getCategory() != null ? product.getCategory().getName() : null,
                product.getImageUrl(),
                null
        );
    }
}
