package com.commerce.user.service;

import com.commerce.common.dto.*;
import com.commerce.common.exception.ServiceException;
import com.commerce.user.entity.Address;
import com.commerce.user.entity.AuthToken;
import com.commerce.user.entity.User;
import com.commerce.user.event.UserEventPublisher;
import com.commerce.user.repository.AddressRepository;
import com.commerce.user.repository.AuthTokenRepository;
import com.commerce.user.repository.UserRepository;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.LocalDateTime;
import java.util.List;
import java.util.UUID;

@Service
public class UserService {

    private final UserRepository userRepository;
    private final AddressRepository addressRepository;
    private final AuthTokenRepository authTokenRepository;
    private final PasswordHasher passwordHasher;
    private final UserEventPublisher eventPublisher;
    private final long tokenTtlHours;

    public UserService(UserRepository userRepository,
                        AddressRepository addressRepository,
                        AuthTokenRepository authTokenRepository,
                        PasswordHasher passwordHasher,
                        UserEventPublisher eventPublisher,
                        @Value("${auth.token-ttl-hours:24}") long tokenTtlHours) {
        this.userRepository = userRepository;
        this.addressRepository = addressRepository;
        this.authTokenRepository = authTokenRepository;
        this.passwordHasher = passwordHasher;
        this.eventPublisher = eventPublisher;
        this.tokenTtlHours = tokenTtlHours;
    }

    @Transactional
    public UserResponse register(UserRegisterRequest request) {
        userRepository.findByEmail(request.email()).ifPresent(u -> {
            throw new ServiceException(HttpStatus.CONFLICT, "Email already registered: " + request.email());
        });

        User user = new User();
        user.setEmail(request.email());
        user.setPasswordHash(passwordHasher.hash(request.password()));
        user.setName(request.name());
        user.setPhone(request.phone());
        userRepository.save(user);

        try {
            eventPublisher.publish(new UserEvent(user.getId(), user.getEmail(), user.getName(), "USER_REGISTERED"));
        } catch (Exception ex) {
            throw new ServiceException(HttpStatus.INTERNAL_SERVER_ERROR,
                    "User registered but failed to record event: " + ex.getMessage());
        }

        return toResponse(user);
    }

    @Transactional
    public LoginResponse login(LoginRequest request) {
        User user = userRepository.findByEmail(request.email())
                .orElseThrow(() -> new ServiceException(HttpStatus.UNAUTHORIZED, "Invalid credentials"));

        if (!passwordHasher.matches(request.password(), user.getPasswordHash())) {
            throw new ServiceException(HttpStatus.UNAUTHORIZED, "Invalid credentials");
        }

        AuthToken authToken = new AuthToken();
        authToken.setUserId(user.getId());
        authToken.setToken(UUID.randomUUID().toString().replace("-", ""));
        LocalDateTime expiresAt = LocalDateTime.now().plusHours(tokenTtlHours);
        authToken.setExpiresAt(expiresAt);
        authTokenRepository.save(authToken);

        return new LoginResponse(authToken.getToken(), user.getId(), expiresAt);
    }

    @Transactional(readOnly = true)
    public TokenVerifyResponse verifyToken(String token) {
        return authTokenRepository.findByToken(token)
                .filter(t -> t.getExpiresAt().isAfter(LocalDateTime.now()))
                .map(t -> userRepository.findById(t.getUserId())
                        .map(u -> new TokenVerifyResponse(true, u.getId(), u.getEmail()))
                        .orElse(new TokenVerifyResponse(false, null, null)))
                .orElse(new TokenVerifyResponse(false, null, null));
    }

    @Transactional(readOnly = true)
    public UserResponse getUser(Long id) {
        return toResponse(findById(id));
    }

    @Transactional(readOnly = true)
    public List<UserResponse> searchUsers(String email, String name) {
        List<User> users;
        if (email != null) {
            users = userRepository.findByEmailContainingIgnoreCase(email);
        } else if (name != null) {
            users = userRepository.findByNameContainingIgnoreCase(name);
        } else {
            users = userRepository.findAll();
        }
        return users.stream().map(this::toResponse).toList();
    }

    @Transactional
    public AddressResponse addAddress(Long userId, AddressRequest request) {
        findById(userId); // 존재 확인

        Address address = new Address();
        address.setUserId(userId);
        address.setRecipientName(request.recipientName());
        address.setPhone(request.phone());
        address.setZipcode(request.zipcode());
        address.setAddress1(request.address1());
        address.setAddress2(request.address2());
        address.setDefault(Boolean.TRUE.equals(request.isDefault()));
        addressRepository.save(address);
        return toAddressResponse(address);
    }

    @Transactional(readOnly = true)
    public List<AddressResponse> getAddresses(Long userId) {
        return addressRepository.findByUserId(userId).stream().map(this::toAddressResponse).toList();
    }

    @Transactional(readOnly = true)
    public AddressResponse getAddress(Long userId, Long addressId) {
        return toAddressResponse(findAddress(userId, addressId));
    }

    @Transactional
    public AddressResponse updateAddress(Long userId, Long addressId, AddressRequest request) {
        Address address = findAddress(userId, addressId);
        address.setRecipientName(request.recipientName());
        address.setPhone(request.phone());
        address.setZipcode(request.zipcode());
        address.setAddress1(request.address1());
        address.setAddress2(request.address2());
        if (request.isDefault() != null) {
            address.setDefault(request.isDefault());
        }
        addressRepository.save(address);
        return toAddressResponse(address);
    }

    @Transactional
    public void deleteAddress(Long userId, Long addressId) {
        Address address = findAddress(userId, addressId);
        addressRepository.delete(address);
    }

    private Address findAddress(Long userId, Long addressId) {
        Address address = addressRepository.findById(addressId)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND, "Address not found: " + addressId));
        if (!address.getUserId().equals(userId)) {
            throw new ServiceException(HttpStatus.NOT_FOUND, "Address not found for user: " + userId);
        }
        return address;
    }

    private User findById(Long id) {
        return userRepository.findById(id)
                .orElseThrow(() -> new ServiceException(HttpStatus.NOT_FOUND, "User not found: " + id));
    }

    private UserResponse toResponse(User user) {
        return new UserResponse(user.getId(), user.getEmail(), user.getName(), user.getPhone(), user.getCreatedAt());
    }

    private AddressResponse toAddressResponse(Address address) {
        return new AddressResponse(
                address.getId(),
                address.getUserId(),
                address.getRecipientName(),
                address.getPhone(),
                address.getZipcode(),
                address.getAddress1(),
                address.getAddress2(),
                address.isDefault()
        );
    }
}
