package com.commerce.user.controller;

import com.commerce.common.dto.*;
import com.commerce.user.service.UserService;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/users")
public class UserController {

    private final UserService userService;

    public UserController(UserService userService) {
        this.userService = userService;
    }

    @PostMapping("/register")
    public ResponseEntity<UserResponse> register(@RequestBody UserRegisterRequest request) {
        return ResponseEntity.ok(userService.register(request));
    }

    @PostMapping("/login")
    public ResponseEntity<LoginResponse> login(@RequestBody LoginRequest request) {
        return ResponseEntity.ok(userService.login(request));
    }

    @PostMapping("/verify-token")
    public ResponseEntity<TokenVerifyResponse> verifyToken(@RequestParam String token) {
        return ResponseEntity.ok(userService.verifyToken(token));
    }

    @GetMapping
    public ResponseEntity<List<UserResponse>> searchUsers(
            @RequestParam(required = false) String email,
            @RequestParam(required = false) String name) {
        return ResponseEntity.ok(userService.searchUsers(email, name));
    }

    @GetMapping("/{id}")
    public ResponseEntity<UserResponse> getUser(@PathVariable Long id) {
        return ResponseEntity.ok(userService.getUser(id));
    }

    @PostMapping("/{userId}/addresses")
    public ResponseEntity<AddressResponse> addAddress(@PathVariable Long userId,
                                                        @RequestBody AddressRequest request) {
        return ResponseEntity.ok(userService.addAddress(userId, request));
    }

    @GetMapping("/{userId}/addresses")
    public ResponseEntity<List<AddressResponse>> getAddresses(@PathVariable Long userId) {
        return ResponseEntity.ok(userService.getAddresses(userId));
    }

    @GetMapping("/{userId}/addresses/{addressId}")
    public ResponseEntity<AddressResponse> getAddress(@PathVariable Long userId, @PathVariable Long addressId) {
        return ResponseEntity.ok(userService.getAddress(userId, addressId));
    }

    @PutMapping("/{userId}/addresses/{addressId}")
    public ResponseEntity<AddressResponse> updateAddress(@PathVariable Long userId, @PathVariable Long addressId,
                                                           @RequestBody AddressRequest request) {
        return ResponseEntity.ok(userService.updateAddress(userId, addressId, request));
    }

    @DeleteMapping("/{userId}/addresses/{addressId}")
    public ResponseEntity<Void> deleteAddress(@PathVariable Long userId, @PathVariable Long addressId) {
        userService.deleteAddress(userId, addressId);
        return ResponseEntity.noContent().build();
    }
}
