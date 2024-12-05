FROM nginx:latest

# Create a non-root user and group
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Copy Nginx configuration file
COPY /configs/nginx/nginx.conf /etc/nginx/nginx.conf

# Change ownership of the Nginx directories
RUN chown -R appuser:appuser /var/cache/nginx /var/log/nginx /var/run /etc/nginx

# Switch to the non-root user
USER appuser

# Expose the Nginx port
EXPOSE 80

# Run Nginx server
CMD ["nginx", "-g", "daemon off;"]