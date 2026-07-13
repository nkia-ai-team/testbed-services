package com.commerce.product.service;

import com.commerce.common.dto.CategoryResponse;
import com.commerce.common.dto.ProductResponse;
import com.commerce.common.dto.ProductVariantResponse;
import com.commerce.common.dto.ReserveStockResponse;
import com.commerce.common.exception.ServiceException;
import com.commerce.product.client.InventoryClient;
import com.commerce.product.entity.Category;
import com.commerce.product.entity.Product;
import com.commerce.product.repository.CategoryRepository;
import com.commerce.product.repository.ProductRepository;
import com.commerce.product.repository.ProductVariantRepository;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

import java.util.List;

@Service
public class ProductService {

    private final ProductRepository productRepository;
    private final CategoryRepository categoryRepository;
    private final ProductVariantRepository productVariantRepository;
    private final InventoryClient inventoryClient;

    public ProductService(ProductRepository productRepository,
                           CategoryRepository categoryRepository,
                           ProductVariantRepository productVariantRepository,
                           InventoryClient inventoryClient) {
        this.productRepository = productRepository;
        this.categoryRepository = categoryRepository;
        this.productVariantRepository = productVariantRepository;
        this.inventoryClient = inventoryClient;
    }

    // 응답은 기존과 동일하게 배열(List) 그대로 유지한다 — page/size는 조회 슬라이싱에만 쓰고
    // Page 메타데이터(totalPages 등)를 감싼 객체로 응답 바디를 바꾸지 않는다. 시나리오
    // 러너·api-gateway 프록시·BFF 등 기존 소비자가 배열을 기대하므로 하위호환을 최우선한다.
    public List<ProductResponse> getProducts(Long categoryId, String keyword, Pageable pageable) {
        Page<Product> products;
        if (categoryId != null && keyword != null) {
            products = productRepository.findByCategoryIdAndNameContainingIgnoreCase(categoryId, keyword, pageable);
        } else if (categoryId != null) {
            products = productRepository.findByCategoryId(categoryId, pageable);
        } else if (keyword != null) {
            products = productRepository.findByNameContainingIgnoreCase(keyword, pageable);
        } else {
            products = productRepository.findAll(pageable);
        }
        return products.map(this::toResponse).getContent();
    }

    public List<ProductVariantResponse> getVariants(Long productId) {
        findById(productId); // 존재 확인 — 없으면 404
        return productVariantRepository.findByProductId(productId).stream()
                .map(v -> new ProductVariantResponse(v.getId(), v.getProductId(), v.getSku(), v.getVariantName(), v.getPriceDelta()))
                .toList();
    }

    public List<CategoryResponse> getCategories() {
        return categoryRepository.findAll().stream()
                .map(c -> new CategoryResponse(c.getId(), c.getName(), c.getDescription()))
                .toList();
    }

    public List<ProductResponse> getProductsByCategory(Long categoryId, Pageable pageable) {
        Category category = categoryRepository.findById(categoryId)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND, "Category not found: " + categoryId));
        return productRepository.findByCategoryId(category.getId(), pageable).map(this::toResponse).getContent();
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
