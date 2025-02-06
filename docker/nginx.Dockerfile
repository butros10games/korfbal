FROM nginx:1.27

# Copy Nginx configuration file
COPY /configs/nginx/nginx.conf /etc/nginx/nginx.conf
COPY /configs/nginx/ssl/ /etc/nginx/ssl/

# Expose the Nginx port
EXPOSE 80

# Run Nginx server
CMD ["nginx", "-g", "daemon off;"]
