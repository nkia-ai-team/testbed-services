FROM eclipse-temurin:17-jdk AS builder
WORKDIR /build

COPY pom.xml ./pom.xml
COPY shop-common ./shop-common
COPY order-service/pom.xml ./order-service/pom.xml
COPY product-service/pom.xml ./product-service/pom.xml
COPY inventory-service/pom.xml ./inventory-service/pom.xml
COPY payment-service/pom.xml ./payment-service/pom.xml
COPY notification-service/pom.xml ./notification-service/pom.xml

RUN apt-get update && apt-get install -y maven && \
    mvn -pl shop-common -am install -DskipTests -q && \
    mvn -pl order-service dependency:go-offline -q

COPY order-service/src ./order-service/src
RUN mvn -pl order-service package -DskipTests -q

FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=builder /build/order-service/target/*.jar app.jar
RUN mkdir -p /opt/apm
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "app.jar"]
